using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace LiveCams;

/// <summary>
/// One borderless window per camera, parented into the desktop wallpaper layer.
/// It loads the camera's official page and restyles it so only the player shows,
/// full-screen. The embed therefore runs exactly as the host site intends.
/// </summary>
internal sealed class CamWindow : Form
{
    // Runs in the official page before its own scripts: hides everything except
    // the camera player and stretches the player over the whole monitor.
    private const string IsolationScriptTemplate = """
        (() => {
          if (window.top !== window || !location.protocol.startsWith('http')) return;
          const SELECTOR = __SELECTOR__, EXTRA = __EXTRA__;
          const css = `
            html, body { margin:0 !important; padding:0 !important; overflow:hidden !important; background:#000 !important; }
            body { visibility:hidden !important; }
            #livecam-frame { visibility:visible !important; display:block !important; position:fixed !important;
              left:0 !important; top:0 !important; width:100vw !important; height:calc(100vh + ${EXTRA}px) !important;
              max-width:none !important; max-height:none !important; min-height:0 !important;
              border:0 !important; margin:0 !important; z-index:2147483647 !important; }`;
          const addStyle = () => {
            if (!document.documentElement || document.getElementById('livecam-style')) return;
            const s = document.createElement('style');
            s.id = 'livecam-style';
            s.textContent = css;
            document.documentElement.appendChild(s);
          };
          const isolate = () => {
            addStyle();
            if (document.getElementById('livecam-frame')) return true;
            const original = document.querySelector(SELECTOR);
            if (!original || !document.body) return false;
            const frame = document.createElement('iframe');
            frame.id = 'livecam-frame';
            frame.name = 'livecam';
            frame.src = original.src;
            frame.allow = 'autoplay; fullscreen';
            frame.setAttribute('allowfullscreen', '');
            // Drop the page's other players so hidden cams don't stream in the background.
            document.querySelectorAll('iframe').forEach(f => f.remove());
            document.body.appendChild(frame);
            return true;
          };
          addStyle();
          const start = () => {
            if (isolate()) return;
            let tries = 0;
            const timer = setInterval(() => { if (isolate() || ++tries > 40) clearInterval(timer); }, 250);
          };
          if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
          else start();
        })();
        """;

    // Runs inside the player iframe. Knows the Surfline and HDOnTap players, falls back to <video>.
    // Also hides the players' control bars (nobody can click a wallpaper); logos stay.
    private const string StatusScript = """
        (() => {
          if (!document.getElementById('livecam-player-style') && document.documentElement) {
            const s = document.createElement('style');
            s.id = 'livecam-player-style';
            s.textContent = '.plyr__controls, .vjs-control-bar { display: none !important; }';
            document.documentElement.appendChild(s);
          }
          const shown = sel => { const e = document.querySelector(sel); return e && e.getClientRects().length ? e : null; };
          if (shown('.sl-cam-player__player-timeout__button')) return 'timeout';
          if (shown('.sl-cam-player__start-controls')) return 'start';
          const vids = [...document.querySelectorAll('video')];
          const v = vids.find(x => !x.paused) || vids.find(x => x.currentSrc) || vids[0];
          if (!v) return 'novideo';
          if (v.error) return 'error';
          if (v.paused) return 'paused';
          return 'playing:' + v.currentTime.toFixed(2);
        })()
        """;

    private const string ResumeScript = """
        (() => {
          const shown = sel => { const e = document.querySelector(sel); return e && e.getClientRects().length ? e : null; };
          // Surfline's handlers sit on the <button> and on the SVG icon (SVG has no .click()), so dispatch.
          const click = e => e.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
          const button = shown('.sl-cam-player__player-timeout__button') || shown('.sl-cam-player__start-controls__icon');
          if (button) { click(button); return 'clicked'; }
          const vids = [...document.querySelectorAll('video')];
          const v = vids.find(x => x.currentSrc) || vids[0];
          const bigPlay = shown('.vjs-big-play-button');
          if (v && v.paused) { v.muted = true; v.play().catch(() => bigPlay && click(bigPlay)); return 'play'; }
          return 'noop';
        })()
        """;

    // Grabs the current video frame at the stream's native resolution (no page overlays).
    // The canvas is reused between grabs to avoid churning 8 MB buffers.
    private const string GrabScript = """
        (() => {
          const vids = [...document.querySelectorAll('video')];
          const v = vids.find(x => !x.paused) || vids[0];
          if (!v || v.paused || !v.videoWidth) return '';
          const c = window.__livecamGrab || (window.__livecamGrab = document.createElement('canvas'));
          c.width = v.videoWidth; c.height = v.videoHeight;
          c.getContext('2d').drawImage(v, 0, 0);
          return c.toDataURL('image/jpeg', 0.92);
        })()
        """;

    // Covers the player with a still of the current frame. The video keeps playing
    // underneath, so frame grabs for the animal logger are unaffected.
    private const string FreezeScript = """
        (() => {
          const vids = [...document.querySelectorAll('video')];
          const v = vids.find(x => !x.paused) || vids[0];
          if (!v || !v.videoWidth) return 'novideo';
          let c = document.getElementById('livecam-freeze');
          if (!c) {
            c = document.createElement('canvas');
            c.id = 'livecam-freeze';
            c.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;object-fit:cover;' +
              'z-index:2147483647;pointer-events:none;background:#000';
            document.body.appendChild(c);
          }
          c.width = v.videoWidth; c.height = v.videoHeight;
          c.getContext('2d').drawImage(v, 0, 0);
          c.style.display = 'block';
          return 'frozen';
        })()
        """;

    private const string UnfreezeScript = """
        (() => { const c = document.getElementById('livecam-freeze'); if (c) c.style.display = 'none'; return 'live'; })()
        """;

    // What the monitor shows right now as a JPEG: the freeze overlay if it's up,
    // otherwise the current video frame (paused or not).
    private const string StillScript = """
        (() => {
          const frozen = document.getElementById('livecam-freeze');
          if (frozen && frozen.style.display !== 'none') return frozen.toDataURL('image/jpeg', 0.92);
          const vids = [...document.querySelectorAll('video')];
          const v = vids.find(x => !x.paused) || vids.find(x => x.videoWidth) || vids[0];
          if (!v || !v.videoWidth) return '';
          const c = document.createElement('canvas');
          c.width = v.videoWidth; c.height = v.videoHeight;
          c.getContext('2d').drawImage(v, 0, 0);
          return c.toDataURL('image/jpeg', 0.92);
        })()
        """;

    // Analytics, ad and captcha scripts the pages load. None are needed to show the
    // cams; blocking them saves CPU, memory, a few processes and network.
    private static readonly string[] BlockedUrls =
    {
        "*://www.google.com/recaptcha/*", "*://www.gstatic.com/recaptcha/*", "*://www.recaptcha.net/*",
        "*://www.googletagmanager.com/*", "*://www.google-analytics.com/*", "*://*.google-analytics.com/*",
        "*://*.doubleclick.net/*", "*://imasdk.googleapis.com/*", "*://cdn.segment.com/*", "*://api.segment.io/*",
        "*://connect.facebook.net/*", "*://*.hotjar.com/*", "*://siteimproveanalytics.com/*", "*://*.siteimprove.com/*",
    };

    private static readonly TimeSpan StallBeforeReload = TimeSpan.FromMinutes(3);

    private readonly CamConfig cam;
    private readonly Rectangle monitor;
    private readonly string? captureDir;
    private readonly WebView2 web;
    private CoreWebView2Frame? camFrame;

    private double lastVideoTime = -1;
    private DateTime lastAdvance = DateTime.UtcNow;
    private DateTime lastReload = DateTime.UtcNow;
    private DateTime lastCapture = DateTime.MinValue;
    private bool wantedPlaying;
    private bool displayFrozen;
    private DateTime liveSince = DateTime.UtcNow;
    private bool suspended;
    private byte[]? suspendedStill;

    public CamConfig Cam => cam;
    public Rectangle Monitor => monitor;
    public string Status { get; private set; } = "starting";
    public bool DisplayFrozen => displayFrozen;
    public bool Suspended => suspended;

    public CamWindow(CamConfig cam, Rectangle monitor, string configDir)
    {
        this.cam = cam;
        this.monitor = monitor;
        if (cam.CaptureEverySeconds > 0 && !string.IsNullOrWhiteSpace(cam.CaptureDir))
            captureDir = Path.GetFullPath(Path.Combine(configDir, cam.CaptureDir));

        Text = $"LiveCams - {cam.Name}";
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.Manual;
        Bounds = monitor;
        BackColor = Color.Black;

        web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.Black };
        Controls.Add(web);
    }

    /// <summary>Parents the window into the wallpaper layer, covering its monitor.</summary>
    public void AttachToDesktop()
    {
        _ = Handle; // create the HWND without showing it as a normal top-level window first
        var (host, defView) = Native.GetWallpaperHost();
        Native.SetParent(Handle, host);
        var r = Native.ScreenToClient(host, monitor);
        Native.SetWindowPos(Handle, defView, r.X, r.Y, r.Width, r.Height,
            Native.SWP_NOACTIVATE | Native.SWP_SHOWWINDOW);
        Log.Write($"[{cam.Name}] attached to desktop at {r} (monitor {monitor})");
    }

    public async Task InitAsync(CoreWebView2Environment env)
    {
        await web.EnsureCoreWebView2Async(env);
        var core = web.CoreWebView2;
        core.IsMuted = true;
        var settings = core.Settings;
        settings.AreDefaultContextMenusEnabled = false;
        settings.IsStatusBarEnabled = false;
        settings.IsZoomControlEnabled = false;
        settings.IsPinchZoomEnabled = false;
        settings.IsSwipeNavigationEnabled = false;
        settings.AreBrowserAcceleratorKeysEnabled = false;

        core.NewWindowRequested += (_, e) => e.Handled = true;
        foreach (string pattern in BlockedUrls)
            core.AddWebResourceRequestedFilter(pattern, CoreWebView2WebResourceContext.All,
                CoreWebView2WebResourceRequestSourceKinds.All);
        core.WebResourceRequested += (_, e) =>
            e.Response = env.CreateWebResourceResponse(null, 403, "Blocked by LiveCams", "");
        core.FrameCreated += OnFrameCreated;
        core.ProcessFailed += (_, e) =>
        {
            Log.Write($"[{cam.Name}] browser process failed: {e.ProcessFailedKind}; reloading");
            Reload();
        };
        core.NavigationCompleted += (_, e) =>
        {
            if (!e.IsSuccess) Log.Write($"[{cam.Name}] navigation failed: {e.WebErrorStatus}");
        };

        string script = IsolationScriptTemplate
            .Replace("__SELECTOR__", JsonSerializer.Serialize(cam.Iframe))
            .Replace("__EXTRA__", cam.ExtraBottomPx.ToString(CultureInfo.InvariantCulture));
        await core.AddScriptToExecuteOnDocumentCreatedAsync(script);
        core.Navigate(cam.Page);
    }

    private void OnFrameCreated(object? sender, CoreWebView2FrameCreatedEventArgs e)
    {
        if (e.Frame.Name != "livecam") return;
        var frame = e.Frame;
        camFrame = frame;
        frame.Destroyed += (_, _) => { if (camFrame == frame) camFrame = null; };
    }

    private async Task<string> RunInPlayerAsync(string script)
    {
        var frame = camFrame;
        if (frame == null) return "noframe";
        try
        {
            string json = await frame.ExecuteScriptAsync(script);
            return JsonSerializer.Deserialize<string>(json) ?? "null";
        }
        catch (Exception ex) when (ex is InvalidOperationException or COMException or JsonException)
        {
            return "noframe";
        }
    }

    /// <summary>
    /// Called every few seconds. <paramref name="watched"/> says whether the user can
    /// currently see this monitor's wallpaper and is at the PC.
    /// </summary>
    public async Task TickAsync(bool watched)
    {
        if (web.CoreWebView2 == null || suspended) return;
        var now = DateTime.UtcNow;

        string status = await RunInPlayerAsync(StatusScript);
        if (StatusKind(status) != StatusKind(Status))
            Log.Write($"[{cam.Name}] {StatusKind(Status)} -> {StatusKind(status)} (watched={watched})");
        Status = status;

        if (status.StartsWith("playing:", StringComparison.Ordinal)
            && double.TryParse(status.AsSpan(8), NumberStyles.Float, CultureInfo.InvariantCulture, out double t)
            && t != lastVideoTime)
        {
            lastVideoTime = t;
            lastAdvance = now;
        }
        bool advancing = now - lastAdvance < TimeSpan.FromSeconds(15);

        bool shouldPlay = !cam.ResumeWhenWatched || watched;
        if (!shouldPlay || !wantedPlaying) lastAdvance = advancing ? lastAdvance : now; // stall clock only runs while we want playback
        wantedPlaying = shouldPlay;

        if (shouldPlay && !advancing)
        {
            if (status is "timeout" or "start" or "paused")
            {
                string result = await RunInPlayerAsync(ResumeScript);
                Log.Write($"[{cam.Name}] resume ({status}) -> {result}");
            }
            if (now - lastAdvance > StallBeforeReload && now - lastReload > StallBeforeReload)
            {
                Log.Write($"[{cam.Name}] no video progress for {StallBeforeReload.TotalMinutes} min (status {status}); reloading");
                Reload();
            }
        }

        if (cam.FreezeDisplayAfterSeconds > 0 && !displayFrozen && advancing
            && now - liveSince >= TimeSpan.FromSeconds(cam.FreezeDisplayAfterSeconds))
        {
            displayFrozen = await RunInPlayerAsync(FreezeScript) == "frozen";
            if (displayFrozen) Log.Write($"[{cam.Name}] display frozen (stream keeps running)");
        }

        if (captureDir != null && advancing && now - lastCapture >= TimeSpan.FromSeconds(cam.CaptureEverySeconds))
        {
            lastCapture = now;
            await CaptureAsync();
        }
    }

    /// <summary>The user asked for this cam (desktop click or tray): resume it, or reload it if the player is broken.</summary>
    public async Task ForceResumeAsync()
    {
        if (suspended) return; // the app wakes suspended cams itself
        var now = DateTime.UtcNow;
        string status = await RunInPlayerAsync(StatusScript);
        bool playing = status.StartsWith("playing:", StringComparison.Ordinal);
        bool broken = status is "noframe" or "novideo" or "error" || (playing && now - lastAdvance > TimeSpan.FromSeconds(20));
        if (broken && now - lastReload > TimeSpan.FromSeconds(20))
        {
            Log.Write($"[{cam.Name}] manual refresh: player {status}; reloading");
            Reload();
            return;
        }
        if (!playing)
        {
            string result = await RunInPlayerAsync(ResumeScript);
            Log.Write($"[{cam.Name}] manual resume ({status}) -> {result}");
        }
        liveSince = now;
        if (displayFrozen)
        {
            await RunInPlayerAsync(UnfreezeScript);
            displayFrozen = false;
            Log.Write($"[{cam.Name}] display live again");
        }
    }

    /// <summary>
    /// Replaces the page with a still of the current picture. The players are unloaded,
    /// so the cam then costs no CPU, GPU or network until <see cref="Wake"/>.
    /// </summary>
    public async Task SuspendAsync()
    {
        if (suspended || web.CoreWebView2 == null) return;
        byte[]? still = await GetStillAsync();
        suspended = true;
        suspendedStill = still;
        camFrame = null;
        string img = still == null ? "" :
            $"<img src='data:image/jpeg;base64,{Convert.ToBase64String(still)}' " +
            "style='width:100vw;height:100vh;object-fit:cover;display:block'>";
        web.CoreWebView2.NavigateToString(
            $"<!doctype html><html><body style='margin:0;background:#000;overflow:hidden'>{img}</body></html>");
        Log.Write($"[{cam.Name}] suspended");
    }

    /// <summary>Reloads the live page after <see cref="SuspendAsync"/>.</summary>
    public void Wake()
    {
        if (!suspended) return;
        suspended = false;
        lastReload = lastAdvance = liveSince = DateTime.UtcNow;
        displayFrozen = false;
        web.CoreWebView2?.Navigate(cam.Page);
        Log.Write($"[{cam.Name}] woke");
    }

    /// <summary>Saves what this monitor shows as a JPEG in <paramref name="dir"/>; null if there is no picture.</summary>
    public async Task<string?> SaveStillAsync(string dir)
    {
        byte[]? still = await GetStillAsync();
        if (still == null) return null;
        Directory.CreateDirectory(dir);
        string prefix = $"still-monitor{cam.Monitor}-";
        foreach (string old in Directory.GetFiles(dir, prefix + "*.jpg"))
            File.Delete(old); // a fresh file name makes Windows re-read the wallpaper
        string path = Path.Combine(dir, $"{prefix}{DateTime.Now:yyyyMMdd-HHmmss}.jpg");
        await File.WriteAllBytesAsync(path, still);
        return path;
    }

    private async Task<byte[]?> GetStillAsync()
    {
        if (suspended) return suspendedStill;
        string dataUrl = await RunInPlayerAsync(StillScript);
        int comma = dataUrl.IndexOf(',');
        if (dataUrl.StartsWith("data:image/jpeg", StringComparison.Ordinal) && comma > 0)
            return Convert.FromBase64String(dataUrl[(comma + 1)..]);
        return null;
    }

    public void Reload()
    {
        if (suspended) return;
        lastReload = lastAdvance = liveSince = DateTime.UtcNow;
        displayFrozen = false; // a reloaded page has no freeze overlay
        camFrame = null;
        web.CoreWebView2?.Reload();
    }

    private async Task CaptureAsync()
    {
        try
        {
            string dataUrl = await RunInPlayerAsync(GrabScript);
            int comma = dataUrl.IndexOf(',');
            if (!dataUrl.StartsWith("data:image/jpeg", StringComparison.Ordinal) || comma < 0) return;
            byte[] jpeg = Convert.FromBase64String(dataUrl[(comma + 1)..]);

            Directory.CreateDirectory(captureDir!);
            string tmp = Path.Combine(captureDir!, "latest.tmp");
            string final = Path.Combine(captureDir!, "latest.jpg");
            await File.WriteAllBytesAsync(tmp, jpeg);
            File.Move(tmp, final, overwrite: true); // the logger only ever sees complete files
        }
        catch (IOException ex)
        {
            Log.Write($"[{cam.Name}] capture skipped: {ex.Message}");
        }
    }

    private static string StatusKind(string status) =>
        status.StartsWith("playing:", StringComparison.Ordinal) ? "playing" : status;

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            cp.ExStyle |= 0x80;        // WS_EX_TOOLWINDOW: keep out of Alt+Tab
            cp.ExStyle |= 0x08000000;  // WS_EX_NOACTIVATE: never steal focus
            return cp;
        }
    }
}
