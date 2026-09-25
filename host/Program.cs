using System.Diagnostics;
using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;

namespace LiveCams;

internal static class Program
{
    public const string MutexName = @"Local\LiveCamsWallpaper";
    public const string OffEventName = @"Local\LiveCamsOff";   // freeze each monitor on a still, then exit
    public const string QuitEventName = @"Local\LiveCamsQuit"; // exit, leaving the normal wallpaper

    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Contains("--off")) { RemoteControl.TurnOff(freeze: true); return; }
        if (args.Contains("--quit")) { RemoteControl.TurnOff(freeze: false); return; }
        if (args.Contains("--review"))
        {
            // The review window on its own, e.g. while the cams are turned off.
            ApplicationConfiguration.Initialize();
            Application.Run(new ReviewForm(Path.GetDirectoryName(AppConfig.Locate())!));
            return;
        }
        if (args.Contains("--on")) Autostart.Enable();

        using var mutex = new Mutex(true, MutexName, out bool owned);
        if (!owned)
        {
            // A restart waits for the old instance to exit; any other second launch just quits.
            try { owned = args.Contains("--restart") && mutex.WaitOne(TimeSpan.FromSeconds(20)); }
            catch (AbandonedMutexException) { owned = true; }
            if (!owned) return;
        }

        ApplicationConfiguration.Initialize();
        Application.Run(new LiveCamsApp());
        mutex.ReleaseMutex();
    }
}

/// <summary>Lets "LiveCams.exe --off" (the OFF .bat) shut down the running instance.</summary>
internal static class RemoteControl
{
    public static void TurnOff(bool freeze)
    {
        Autostart.Disable();
        if (!EventWaitHandle.TryOpenExisting(freeze ? Program.OffEventName : Program.QuitEventName, out var signal))
            return; // not running
        using (signal) signal.Set();

        // Wait until the running instance has saved the stills and exited.
        using var mutex = new Mutex(false, Program.MutexName);
        try
        {
            if (mutex.WaitOne(TimeSpan.FromSeconds(30))) mutex.ReleaseMutex();
        }
        catch (AbandonedMutexException)
        {
            mutex.ReleaseMutex();
        }
    }
}

internal static class Autostart
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";

    public static void Enable()
    {
        using var key = Registry.CurrentUser.CreateSubKey(RunKey);
        key.SetValue("LiveCams", $"\"{Environment.ProcessPath}\"");
    }

    public static void Disable()
    {
        using var key = Registry.CurrentUser.CreateSubKey(RunKey);
        key.DeleteValue("LiveCams", throwOnMissingValue: false);
    }
}

/// <summary>Owns the camera windows, the tray icon, the tracker process, and the "is anyone watching" loop.</summary>
internal sealed class LiveCamsApp : ApplicationContext
{
    private static readonly TimeSpan TickInterval = TimeSpan.FromSeconds(3);
    private static readonly TimeSpan GameOverDelay = TimeSpan.FromSeconds(20);

    private readonly AppConfig config;
    private readonly string configDir;
    private readonly List<CamWindow> windows = new();
    private readonly NotifyIcon tray;
    private readonly System.Windows.Forms.Timer timer = new();
    private readonly ShellWatcher shellWatcher;
    private readonly DesktopClickWatcher desktopClicks;
    private readonly TrackerProcess? tracker;
    private readonly EventWaitHandle offSignal = new(false, EventResetMode.AutoReset, Program.OffEventName);
    private readonly EventWaitHandle quitSignal = new(false, EventResetMode.AutoReset, Program.QuitEventName);
    private CoreWebView2Environment? env;
    private bool ticking;
    private bool exiting;
    private bool wallpaperFrozen;
    private bool gameMode;
    private DateTime lastFullscreenSeen;
    private DateTime lastEfficiencyPass;
    private ReviewForm? reviewForm;

    public LiveCamsApp()
    {
        string configPath = AppConfig.Locate();
        configDir = Path.GetDirectoryName(configPath)!;
        Log.Init(Path.Combine(configDir, "logs"));
        config = AppConfig.Load(configPath);
        Log.Write($"starting; config {configPath}");
        Native.SetEfficiencyMode(Environment.ProcessId);

        tray = new NotifyIcon { Icon = MakeTrayIcon(), Text = "LiveCams", Visible = true, ContextMenuStrip = new ContextMenuStrip() };
        tray.ContextMenuStrip.Opening += (_, _) => RebuildMenu();
        tray.DoubleClick += async (_, _) => { foreach (var w in windows) await w.ForceResumeAsync(); };

        // Clicking empty desktop on a cam's monitor resumes that cam (or reloads it if it's broken).
        desktopClicks = new DesktopClickWatcher(async pt =>
        {
            foreach (var w in windows.Where(w => w.Monitor.Contains(pt)))
                await w.ForceResumeAsync();
        });

        // Explorer restarts destroy the wallpaper layer, and monitor changes move it: rebuild from scratch.
        shellWatcher = new ShellWatcher(() => Restart("Explorer restarted"));
        SystemEvents.DisplaySettingsChanged += OnDisplaySettingsChanged;
        SystemEvents.PowerModeChanged += OnPowerModeChanged;

        var ui = SynchronizationContext.Current!;
        ThreadPool.RegisterWaitForSingleObject(offSignal, (_, _) => ui.Post(_ => _ = TurnOffAsync(freeze: true), null), null, -1, true);
        ThreadPool.RegisterWaitForSingleObject(quitSignal, (_, _) => ui.Post(_ => _ = TurnOffAsync(freeze: false), null), null, -1, true);

        if (config.Tracker.Enabled)
            tracker = new TrackerProcess(config.Tracker, configDir);

        timer.Interval = (int)TickInterval.TotalMilliseconds;
        timer.Tick += async (_, _) => await TickAsync();

        _ = StartAsync();
    }

    private async Task StartAsync()
    {
        try
        {
            string args =
                "--autoplay-policy=no-user-gesture-required " +
                // Keep decoding while the wallpaper is behind other windows (the animal logger needs frames).
                "--disable-features=CalculateNativeWinOcclusion,HardwareMediaKeyHandling " +
                "--disable-background-timer-throttling --disable-renderer-backgrounding " +
                "--disable-backgrounding-occluded-windows";
            if (config.DebugPort > 0) args += $" --remote-debugging-port={config.DebugPort}";
            env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(configDir, "host", "webview-data"),
                new CoreWebView2EnvironmentOptions(args));
            env.ProcessInfosChanged += (_, _) => ApplyEfficiencyMode();

            var screens = Screen.AllScreens.OrderBy(s => s.Bounds.X).ThenBy(s => s.Bounds.Y).ToArray();
            foreach (var cam in config.Cams)
            {
                if (cam.Monitor < 1 || cam.Monitor > screens.Length)
                {
                    Log.Write($"[{cam.Name}] monitor {cam.Monitor} not connected ({screens.Length} monitors); skipped");
                    continue;
                }
                var window = new CamWindow(cam, screens[cam.Monitor - 1].Bounds, configDir);
                window.AttachToDesktop();
                window.Show();
                windows.Add(window);
                await window.InitAsync(env);
            }
            tracker?.Start();
            ApplyEfficiencyMode();
            timer.Start();
        }
        catch (Exception ex)
        {
            Log.Write($"startup failed: {ex}");
            tray.ShowBalloonTip(10000, "LiveCams failed to start", ex.Message, ToolTipIcon.Error);
        }
    }

    /// <summary>Efficiency mode for every browser process (they come and go) and the tracker.</summary>
    private void ApplyEfficiencyMode()
    {
        lastEfficiencyPass = DateTime.UtcNow;
        if (env != null)
            foreach (var info in env.GetProcessInfos())
                Native.SetEfficiencyMode(info.ProcessId);
        if (tracker?.ProcessId is int pid) Native.SetEfficiencyMode(pid);
    }

    private async Task TickAsync()
    {
        if (ticking || exiting) return;
        ticking = true;
        try
        {
            // Chromium adjusts its own process priorities now and then; keep them pinned low.
            if (DateTime.UtcNow - lastEfficiencyPass > TimeSpan.FromMinutes(1)) ApplyEfficiencyMode();
            tracker?.KeepAlive();

            await UpdateGameModeAsync();
            if (!gameMode)
            {
                bool userPresent = Native.UserIdleTime() < TimeSpan.FromSeconds(config.WatchedIdleSeconds);
                foreach (var w in windows)
                {
                    bool watched = userPresent && Native.LargestCoverFraction(w.Monitor, out _) < config.CoverThreshold;
                    await w.TickAsync(watched);
                }
            }
            UpdateTrayText();
        }
        catch (Exception ex)
        {
            Log.Write($"tick failed: {ex}");
        }
        finally
        {
            ticking = false;
        }
    }

    /// <summary>
    /// While a game (or any app) runs full-screen, unload the players entirely: each monitor
    /// keeps a still, and the cams, tracker frames and mouse hook cost nothing.
    /// </summary>
    private async Task UpdateGameModeAsync()
    {
        if (!config.PauseDuringFullscreenApps) return;
        var now = DateTime.UtcNow;
        if (Native.FullscreenAppRunning())
        {
            lastFullscreenSeen = now;
            if (gameMode) return;
            gameMode = true;
            Log.Write("full-screen app detected: suspending cams");
            desktopClicks.Enabled = false;
            foreach (var w in windows) await w.SuspendAsync();
        }
        else if (gameMode && now - lastFullscreenSeen > GameOverDelay)
        {
            gameMode = false;
            Log.Write("full-screen app gone: waking cams");
            desktopClicks.Enabled = true;
            foreach (var w in windows) w.Wake();
        }
    }

    private void UpdateTrayText()
    {
        string text = gameMode
            ? "LiveCams: paused for full-screen app"
            : string.Join("\n", windows.Select(w => $"{w.Cam.Name}: {Short(w.Status)}{(w.DisplayFrozen ? " (frozen)" : "")}"));
        tray.Text = text.Length > 127 ? text[..127] : text;
    }

    private static string Short(string status) =>
        status.StartsWith("playing:", StringComparison.Ordinal) ? "live" : status;

    private void RebuildMenu()
    {
        var items = tray.ContextMenuStrip!.Items;
        items.Clear();
        int pending = ReviewForm.PendingCount(configDir);
        var review = items.Add($"Review uncertain sightings ({pending})...", null, (_, _) => OpenReview());
        review.Enabled = pending > 0 || reviewForm != null;
        items.Add(new ToolStripSeparator());
        foreach (var w in windows)
            items.Add($"Resume {w.Cam.Name}  ({Short(w.Status)})", null, async (_, _) => await w.ForceResumeAsync());
        items.Add("Reload cams", null, (_, _) => windows.ForEach(w => w.Reload()));
        items.Add(new ToolStripSeparator());
        items.Add("Open LiveCams folder", null, (_, _) => Process.Start("explorer.exe", configDir));
        items.Add("Turn off (freeze wallpapers)", null, async (_, _) =>
        {
            Autostart.Disable();
            await TurnOffAsync(freeze: true);
        });
    }

    private void OpenReview()
    {
        if (reviewForm is { IsDisposed: false })
        {
            reviewForm.Activate();
            return;
        }
        reviewForm = new ReviewForm(configDir);
        reviewForm.FormClosed += (_, _) => reviewForm = null;
        reviewForm.Show();
    }

    /// <summary>
    /// Shuts everything down. With <paramref name="freeze"/>, each monitor first gets its
    /// current cam picture as a normal Windows wallpaper, so nothing needs to keep running.
    /// </summary>
    private async Task TurnOffAsync(bool freeze)
    {
        if (exiting) return;
        exiting = true;
        timer.Stop();
        if (freeze)
        {
            string stillsDir = Path.Combine(configDir, "frames", "stills");
            foreach (var w in windows)
            {
                try
                {
                    string? still = await w.SaveStillAsync(stillsDir);
                    if (still != null && StaticWallpaper.SetForMonitor(w.Monitor, still))
                    {
                        wallpaperFrozen = true;
                        Log.Write($"[{w.Cam.Name}] frozen as wallpaper: {Path.GetFileName(still)}");
                    }
                }
                catch (Exception ex)
                {
                    Log.Write($"[{w.Cam.Name}] could not freeze: {ex.Message}");
                }
            }
        }
        ExitThread();
    }

    private void OnDisplaySettingsChanged(object? sender, EventArgs e) => Restart("display settings changed");

    private void OnPowerModeChanged(object sender, PowerModeChangedEventArgs e)
    {
        if (e.Mode == PowerModes.Resume)
        {
            Log.Write("resumed from sleep; reloading cams");
            windows.ForEach(w => w.Reload());
        }
    }

    private void Restart(string reason)
    {
        if (exiting) return;
        exiting = true;
        Log.Write($"restarting: {reason}");
        Process.Start(Environment.ProcessPath!, "--restart");
        ExitThread();
    }

    protected override void ExitThreadCore()
    {
        exiting = true;
        timer.Stop();
        SystemEvents.DisplaySettingsChanged -= OnDisplaySettingsChanged;
        SystemEvents.PowerModeChanged -= OnPowerModeChanged;
        shellWatcher.DestroyHandle();
        desktopClicks.Dispose();
        tracker?.Stop();
        foreach (var w in windows) w.Dispose();
        tray.Visible = false;
        tray.Dispose();
        if (!wallpaperFrozen) Native.RefreshStaticWallpaper(); // repaint the normal wallpaper where the cams were
        Log.Write("exited");
        base.ExitThreadCore();
    }

    private static Icon MakeTrayIcon()
    {
        using var bmp = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.FillEllipse(new SolidBrush(Color.FromArgb(0, 105, 148)), 1, 1, 30, 30);
            using var pen = new Pen(Color.White, 3);
            g.DrawBezier(pen, 5, 18, 10, 10, 15, 26, 20, 16);
            g.DrawBezier(pen, 20, 16, 23, 11, 26, 16, 28, 14);
        }
        return Icon.FromHandle(bmp.GetHicon());
    }
}

/// <summary>Hidden top-level window that hears Explorer's "TaskbarCreated" broadcast.</summary>
internal sealed class ShellWatcher : NativeWindow
{
    private readonly uint taskbarCreated = Native.RegisterWindowMessage("TaskbarCreated");
    private readonly Action onExplorerRestart;

    public ShellWatcher(Action onExplorerRestart)
    {
        this.onExplorerRestart = onExplorerRestart;
        CreateHandle(new CreateParams { Caption = "LiveCamsShellWatcher" });
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == taskbarCreated) onExplorerRestart();
        base.WndProc(ref m);
    }
}
