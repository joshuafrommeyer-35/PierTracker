using System.Diagnostics;
using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;

namespace LiveCams;

internal static class Program
{
    public const string MutexName = @"Local\LiveCamsWallpaper";
    public const string OffEventName = @"Local\LiveCamsOff";   // freeze each monitor on a still, then exit
    public const string QuitEventName = @"Local\LiveCamsQuit"; // exit, leaving the normal wallpaper
    public const string PauseEventName = @"Local\LiveCamsPause";   // cams on a still + tracker stopped, until resumed
    public const string ResumeEventName = @"Local\LiveCamsResume";
    /// <summary>How long to wait for the running instance to exit (longer than LiveCamsApp.ShutdownLimit).</summary>
    public static readonly TimeSpan ExitWait = TimeSpan.FromSeconds(60);

    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Contains("--off")) { RemoteControl.TurnOff(freeze: true); return; }
        if (args.Contains("--quit")) { RemoteControl.TurnOff(freeze: false); return; }
        if (args.Contains("--relaunch")) { RemoteControl.Relaunch(); return; }
        if (args.Contains("--pause")) { RemoteControl.SetPaused(true); return; }
        if (args.Contains("--resume")) { RemoteControl.SetPaused(false); return; }
        if (args.Contains("--review"))
        {
            // The review window on its own, e.g. while the cams are turned off.
            ApplicationConfiguration.Initialize();
            Application.Run(new ReviewForm(Path.GetDirectoryName(AppConfig.Locate())!));
            return;
        }
        if (args.Contains("--on"))
        {
            Autostart.Enable();
            RemoteControl.ClearPausedFlag(); // turning the cams on means on, not paused
        }

        using var mutex = new Mutex(true, MutexName, out bool owned);
        if (!owned)
        {
            // A restart waits for the old instance to exit (a stuck shutdown is cut off after
            // LiveCamsApp.ShutdownLimit); any other second launch just quits.
            bool restart = args.Contains("--restart");
            try { owned = restart && mutex.WaitOne(ExitWait); }
            catch (AbandonedMutexException) { owned = true; }
            if (!owned)
            {
                if (restart)
                {
                    Log.Init(Path.Combine(Path.GetDirectoryName(AppConfig.Locate())!, "logs"));
                    Log.Write("restart gave up: the previous instance never exited");
                }
                return;
            }
        }

        ApplicationConfiguration.Initialize();
        // Nobody's there to click an error dialog on a wallpaper: log errors instead.
        Application.ThreadException += (_, e) => Log.Write($"error: {e.Exception}");
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Log.Write($"crashed: {e.ExceptionObject}");
        Application.Run(new LiveCamsApp());
        mutex.ReleaseMutex();
    }
}

/// <summary>Lets "LiveCams.exe --off" (the OFF .bat) shut down the running instance.</summary>
internal static class RemoteControl
{
    /// <summary>The flag that keeps LiveCams paused across restarts, next to livecams.json.</summary>
    public static string PausedFlag => Path.Combine(Path.GetDirectoryName(AppConfig.Locate())!, "paused");

    public static void ClearPausedFlag()
    {
        try { File.Delete(PausedFlag); } catch (IOException) { }
    }

    /// <summary>--pause / --resume: tells the running instance, or just sets the flag for the next start.</summary>
    public static void SetPaused(bool paused)
    {
        if (paused) File.WriteAllText(PausedFlag, "");
        else ClearPausedFlag();
        if (EventWaitHandle.TryOpenExisting(paused ? Program.PauseEventName : Program.ResumeEventName, out var signal))
            using (signal) signal.Set();
    }

    /// <summary>--relaunch: restart the running instance (e.g. after an update) without touching the
    /// start-at-login setting or a pause. Waits until the old one has fully exited.</summary>
    public static void Relaunch()
    {
        TurnOff(freeze: false, keepAutostart: true);
        Process.Start(new ProcessStartInfo(Environment.ProcessPath!)
        {
            UseShellExecute = false,
            WorkingDirectory = Path.GetDirectoryName(Environment.ProcessPath!)!,
        });
    }

    public static void TurnOff(bool freeze, bool keepAutostart = false)
    {
        if (!keepAutostart) Autostart.Disable();
        if (!EventWaitHandle.TryOpenExisting(freeze ? Program.OffEventName : Program.QuitEventName, out var signal))
            return; // not running
        using (signal) signal.Set();

        // Wait until the running instance has saved the stills and exited.
        using var mutex = new Mutex(false, Program.MutexName);
        try
        {
            if (mutex.WaitOne(Program.ExitWait)) mutex.ReleaseMutex();
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
    private static readonly TimeSpan ReviewCheckInterval = TimeSpan.FromMinutes(1);
    private static readonly TimeSpan NoReminderAfterStart = TimeSpan.FromMinutes(15);
    /// <summary>How long shutting down may take (the tracker alone gets up to 15 s) before it's cut off.</summary>
    public static readonly TimeSpan ShutdownLimit = TimeSpan.FromSeconds(30);
    /// <summary>A rebuild because the cams fell off the desktop happens at most this often.</summary>
    private static readonly TimeSpan RebuildAfterStart = TimeSpan.FromMinutes(1);

    private readonly AppConfig config;
    private readonly string configDir;
    private readonly List<CamWindow> windows = new();
    private readonly NotifyIcon tray;
    private readonly Icon trayIcon = MakeTrayIcon(badge: false), trayIconBadge = MakeTrayIcon(badge: true),
                          trayIconPaused = MakeTrayIcon(badge: false, paused: true);
    private readonly DateTime startedAt = DateTime.UtcNow;
    private readonly System.Windows.Forms.Timer timer = new();
    private readonly ShellWatcher shellWatcher;
    private readonly DesktopClickWatcher desktopClicks;
    private readonly TrackerProcess? tracker;
    private readonly EventWaitHandle offSignal = new(false, EventResetMode.AutoReset, Program.OffEventName);
    private readonly EventWaitHandle quitSignal = new(false, EventResetMode.AutoReset, Program.QuitEventName);
    private readonly EventWaitHandle pauseSignal = new(false, EventResetMode.AutoReset, Program.PauseEventName);
    private readonly EventWaitHandle resumeSignal = new(false, EventResetMode.AutoReset, Program.ResumeEventName);
    private bool paused;
    private CoreWebView2Environment? env;
    private bool ticking;
    private bool exiting;
    private System.Threading.Timer? shutdownWatchdog;
    private bool wallpaperFrozen;
    private bool gameMode;
    private DateTime lastFullscreenSeen;
    private DateTime lastEfficiencyPass;
    private ReviewForm? reviewForm;
    private int pendingReviews;
    private DateTime lastReviewCheck;

    public LiveCamsApp()
    {
        string configPath = AppConfig.Locate();
        configDir = Path.GetDirectoryName(configPath)!;
        Log.Init(Path.Combine(configDir, "logs"));
        config = AppConfig.Load(configPath);
        Log.Write($"starting; config {configPath}");
        Native.SetEfficiencyMode(Environment.ProcessId);

        tray = new NotifyIcon { Icon = trayIcon, Text = "LiveCams", Visible = true, ContextMenuStrip = new ContextMenuStrip() };
        tray.ContextMenuStrip.Opening += (_, _) => RebuildMenu();
        tray.BalloonTipClicked += (_, _) => OpenReview();
        tray.DoubleClick += async (_, _) => { foreach (var w in windows) await w.ForceResumeAsync(); };

        // Clicking empty desktop on a cam's monitor resumes that cam (or reloads it if it's broken).
        desktopClicks = new DesktopClickWatcher(async pt =>
        {
            foreach (var w in windows.Where(w => w.Monitor.Contains(pt)))
                await w.ForceResumeAsync();
        });

        // Explorer restarts destroy the wallpaper layer, and monitor changes move it: rebuild from scratch.
        // Posted, not run inside the watcher's window procedure, which would swallow any error.
        var ui = SynchronizationContext.Current!;
        shellWatcher = new ShellWatcher(() => ui.Post(_ => Restart("Explorer restarted"), null));
        SystemEvents.DisplaySettingsChanged += OnDisplaySettingsChanged;
        SystemEvents.PowerModeChanged += OnPowerModeChanged;

        ThreadPool.RegisterWaitForSingleObject(offSignal, (_, _) => ui.Post(_ => _ = TurnOffAsync(freeze: true), null), null, -1, true);
        ThreadPool.RegisterWaitForSingleObject(quitSignal, (_, _) => ui.Post(_ => _ = TurnOffAsync(freeze: false), null), null, -1, true);
        ThreadPool.RegisterWaitForSingleObject(pauseSignal, (_, _) => ui.Post(_ => _ = PauseAsync(), null), null, -1, false);
        ThreadPool.RegisterWaitForSingleObject(resumeSignal, (_, _) => ui.Post(_ => _ = ResumeAsync(), null), null, -1, false);

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

            // Right after Explorer crashes, the desktop can take a while to come back.
            for (int i = 0; i < 60 && !Native.DesktopReady(); i++) await Task.Delay(1000);

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
            if (File.Exists(RemoteControl.PausedFlag))
                await PauseAsync(); // paused before a restart or reboot: stay paused
            else
                tracker?.Start();
            ApplyEfficiencyMode();
            timer.Start();
        }
        catch (Exception ex)
        {
            Log.Write($"startup failed: {ex}");
            if (exiting) return; // cut short by a restart already under way
            tray.ShowBalloonTip(10000, "LiveCams failed to start", ex.Message + " Trying again in a minute.", ToolTipIcon.Error);
            await Task.Delay(TimeSpan.FromMinutes(1));
            Restart("retrying after a failed start");
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
            // Explorer throwing the wallpaper layer away without a restart notice would leave the plain
            // wallpaper showing: rebuild (at most once a minute).
            if (DateTime.UtcNow - startedAt > RebuildAfterStart && windows.FirstOrDefault(w => !w.OnDesktop) is { } lost)
            {
                Restart($"[{lost.Cam.Name}] is no longer on the desktop");
                return;
            }

            // Chromium adjusts its own process priorities now and then; keep them pinned low.
            if (DateTime.UtcNow - lastEfficiencyPass > TimeSpan.FromMinutes(1)) ApplyEfficiencyMode();
            tracker?.RunNightlyIfDue(); // paused too: under a minute at idle priority, once a day
            if (!paused) tracker?.KeepAlive();

            if (!paused) await UpdateGameModeAsync();
            if (!gameMode && !paused)
            {
                bool userPresent = Native.UserIdleTime() < TimeSpan.FromSeconds(config.WatchedIdleSeconds);
                foreach (var w in windows)
                {
                    bool watched = userPresent && Native.LargestCoverFraction(w.Monitor, out _) < config.CoverThreshold;
                    await w.TickAsync(watched);
                }
            }
            CheckReviews();
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
        if (Native.FullscreenAppRunning(config.NotGames))
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

    /// <summary>
    /// Pause (for a demanding game, or any time): each cam shows a still and unloads, and the tracker
    /// stops and frees its memory. It stays paused, across restarts too, until resumed.
    /// </summary>
    private async Task PauseAsync()
    {
        if (paused || exiting) return;
        paused = true;
        File.WriteAllText(RemoteControl.PausedFlag, "");
        Log.Write("paused (cams + tracker)");
        desktopClicks.Enabled = false;
        foreach (var w in windows) await w.SuspendAsync("LiveCams paused · resume from the tray icon");
        tracker?.Stop();
        gameMode = false; // detection starts fresh on resume
        UpdateTrayIcon();
        UpdateTrayText();
    }

    private Task ResumeAsync()
    {
        if (!paused || exiting) return Task.CompletedTask;
        paused = false;
        RemoteControl.ClearPausedFlag();
        Log.Write("resumed (cams + tracker)");
        if (config.PauseDuringFullscreenApps && Native.FullscreenAppRunning(config.NotGames))
        {
            gameMode = true; // a game is still up: the cams wake when it closes
            lastFullscreenSeen = DateTime.UtcNow;
        }
        else
        {
            desktopClicks.Enabled = true;
            foreach (var w in windows) w.Wake();
        }
        tracker?.Start();
        UpdateTrayIcon();
        UpdateTrayText();
        return Task.CompletedTask;
    }

    private void UpdateTrayIcon()
    {
        var icon = paused ? trayIconPaused : pendingReviews > 0 ? trayIconBadge : trayIcon;
        if (tray.Icon != icon) tray.Icon = icon;
    }

    private void UpdateTrayText()
    {
        string text = paused
            ? "LiveCams: paused (cams + tracker). Right-click to resume."
            : gameMode
            ? "LiveCams: paused for full-screen app"
            : string.Join("\n", windows.Select(w => $"{w.Cam.Name}: {Short(w.Status)}{(w.DisplayFrozen ? " (frozen)" : "")}"));
        if (pendingReviews > 0) text += $"\n{pendingReviews} sightings to review";
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
        if (paused)
        {
            items.Add("Resume cams + tracker", null, async (_, _) => await ResumeAsync());
        }
        else
        {
            items.Add("Pause cams + tracker (for games)", null, async (_, _) => await PauseAsync());
            foreach (var w in windows)
                items.Add($"Resume {w.Cam.Name}  ({Short(w.Status)})", null, async (_, _) => await w.ForceResumeAsync());
            items.Add("Reload cams", null, (_, _) => windows.ForEach(w => w.Reload()));
        }
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
        reviewForm.FormClosed += (_, _) =>
        {
            reviewForm = null;
            lastReviewCheck = DateTime.MinValue; // clear the dot right away if the queue is empty now
        };
        reviewForm.Show();
    }

    /// <summary>
    /// Sightings waiting for a person: a dot on the tray icon, plus now and then a notification.
    /// The notification only comes while you're at the PC and not in a game, and never twice
    /// within <see cref="AppConfig.ReviewReminderHours"/> (remembered across restarts).
    /// </summary>
    private void CheckReviews()
    {
        var now = DateTime.UtcNow;
        if (now - lastReviewCheck < ReviewCheckInterval) return;
        lastReviewCheck = now;
        pendingReviews = ReviewForm.PendingCount(configDir);
        UpdateTrayIcon();

        if (config.ReviewReminderHours <= 0 || pendingReviews < config.ReviewReminderMinPending) return;
        if (gameMode || paused || reviewForm != null || now - startedAt < NoReminderAfterStart) return;
        if (Native.UserIdleTime() > TimeSpan.FromMinutes(1) || Native.FullscreenAppRunning()) return;
        string stamp = Path.Combine(configDir, "data", "review", "last_reminder.txt");
        if (File.Exists(stamp) && DateTime.TryParse(File.ReadAllText(stamp), null,
                System.Globalization.DateTimeStyles.RoundtripKind, out var last) &&
            now - last < TimeSpan.FromHours(config.ReviewReminderHours))
            return;
        File.WriteAllText(stamp, now.ToString("o"));
        Log.Write($"review reminder: {pendingReviews} waiting");
        tray.ShowBalloonTip(10000, "Pier sightings to review",
            $"{pendingReviews} sightings are waiting for a look. Click here to open the review window.", ToolTipIcon.None);
    }

    /// <summary>
    /// Shuts everything down. With <paramref name="freeze"/>, each monitor first gets its
    /// current cam picture as a normal Windows wallpaper, so nothing needs to keep running.
    /// </summary>
    private async Task TurnOffAsync(bool freeze)
    {
        if (exiting) return;
        BeginShutdown();
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
        Log.Write($"restarting: {reason}");
        try { Process.Start(Environment.ProcessPath!, "--restart"); }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            Log.Write($"restart failed: {ex.Message}"); // keep running as is
            return;
        }
        BeginShutdown();
        ExitThread();
    }

    /// <summary>
    /// Shutting down must always finish: until this instance exits it holds the lock a new one waits
    /// for, while showing nothing. On 2026-09-26 a cam's browser died with Explorer, closing it threw,
    /// and the old instance sat there all night. A shutdown that hasn't finished in time is cut off.
    /// </summary>
    private void BeginShutdown()
    {
        exiting = true;
        shutdownWatchdog ??= new System.Threading.Timer(_ =>
        {
            Log.Write("shutdown stuck; forcing exit");
            Process.GetCurrentProcess().Kill();
        }, null, ShutdownLimit, Timeout.InfiniteTimeSpan);
    }

    protected override void ExitThreadCore()
    {
        BeginShutdown();
        // Step by step: one that fails (e.g. closing a cam whose browser already died) is logged, and
        // the rest still run.
        ShutdownStep("timer", timer.Stop);
        ShutdownStep("system events", () =>
        {
            SystemEvents.DisplaySettingsChanged -= OnDisplaySettingsChanged;
            SystemEvents.PowerModeChanged -= OnPowerModeChanged;
        });
        ShutdownStep("Explorer watcher", shellWatcher.DestroyHandle);
        ShutdownStep("desktop clicks", desktopClicks.Dispose);
        ShutdownStep("tracker", () => tracker?.Stop());
        foreach (var w in windows) ShutdownStep($"[{w.Cam.Name}] window", w.Dispose);
        ShutdownStep("tray icon", () =>
        {
            tray.Visible = false;
            tray.Dispose();
            trayIcon.Dispose();
            trayIconBadge.Dispose();
            trayIconPaused.Dispose();
        });
        // Repaint the normal wallpaper where the cams were.
        if (!wallpaperFrozen) ShutdownStep("wallpaper", () => Native.RefreshStaticWallpaper());
        Log.Write("exited");
        base.ExitThreadCore();
    }

    private static void ShutdownStep(string what, Action step)
    {
        try { step(); }
        catch (Exception ex) { Log.Write($"shutdown: {what} failed ({ex.GetType().Name}: {ex.Message})"); }
    }

    /// <param name="badge">Adds an amber dot: sightings are waiting for review.</param>
    /// <param name="paused">Grey: cams and tracker paused.</param>
    private static Icon MakeTrayIcon(bool badge, bool paused = false)
    {
        using var bmp = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.FillEllipse(new SolidBrush(paused ? Color.FromArgb(120, 120, 120) : Color.FromArgb(0, 105, 148)), 1, 1, 30, 30);
            using var pen = new Pen(Color.White, 3);
            g.DrawBezier(pen, 5, 18, 10, 10, 15, 26, 20, 16);
            g.DrawBezier(pen, 20, 16, 23, 11, 26, 16, 28, 14);
            if (badge)
            {
                g.FillEllipse(new SolidBrush(Color.FromArgb(255, 176, 32)), 18, 0, 14, 14);
                g.DrawEllipse(new Pen(Color.FromArgb(40, 40, 40), 1.5f), 18, 0, 14, 14);
            }
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

    // A NativeWindow drops errors from its window procedure silently: at least log them.
    protected override void OnThreadException(Exception e) => Log.Write($"error: {e}");
}
