using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Text;

namespace LiveCams;

// Win32 interop: parenting into the desktop wallpaper layer, and deciding
// whether a monitor's wallpaper is actually visible to the user.
internal static class Native
{
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT
    {
        public int Left, Top, Right, Bottom;
        public Rectangle ToRectangle() => Rectangle.FromLTRB(Left, Top, Right, Bottom);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LASTINPUTINFO
    {
        public uint cbSize;
        public uint dwTime;
    }

    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr FindWindow(string? lpClassName, string? lpWindowName);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr FindWindowEx(IntPtr parent, IntPtr childAfter, string? className, string? windowTitle);

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam,
        uint flags, uint timeout, out IntPtr result);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern IntPtr SetParent(IntPtr child, IntPtr newParent);

    [DllImport("user32.dll")]
    private static extern IntPtr GetAncestor(IntPtr hWnd, uint flags);

    [DllImport("user32.dll")]
    private static extern IntPtr GetDesktopWindow();

    [DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr hWnd);

    /// <summary>True while <paramref name="hWnd"/> still sits inside another window (e.g. the wallpaper
    /// layer), not on its own. (GetParent can't tell: it's null for a window without WS_CHILD.)</summary>
    public static bool HasParentWindow(IntPtr hWnd)
    {
        IntPtr parent = GetAncestor(hWnd, 1 /* GA_PARENT */);
        return parent != IntPtr.Zero && parent != GetDesktopWindow() && IsWindow(parent);
    }

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr insertAfter, int x, int y, int cx, int cy, uint flags);

    [DllImport("user32.dll")]
    private static extern int MapWindowPoints(IntPtr from, IntPtr to, ref RECT rect, uint points);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr hWnd, StringBuilder name, int max);

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int index);

    [DllImport("user32.dll")]
    private static extern bool GetLastInputInfo(ref LASTINPUTINFO info);

    [DllImport("dwmapi.dll")]
    private static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out RECT value, int size);

    [DllImport("dwmapi.dll")]
    private static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out int value, int size);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool SystemParametersInfo(uint action, uint param, StringBuilder pvParam, uint winIni);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool SystemParametersInfo(uint action, uint param, string pvParam, uint winIni);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern uint RegisterWindowMessage(string name);

    private const uint SMTO_NORMAL = 0x0000;
    private const int GWL_EXSTYLE = -20;
    private const long WS_EX_TRANSPARENT = 0x20;
    private const long WS_EX_TOOLWINDOW = 0x80;
    private const long WS_EX_NOACTIVATE = 0x08000000;
    private const int DWMWA_EXTENDED_FRAME_BOUNDS = 9;
    private const int DWMWA_CLOAKED = 14;
    public const uint SWP_NOACTIVATE = 0x0010;
    public const uint SWP_SHOWWINDOW = 0x0040;
    private const uint SPI_GETDESKWALLPAPER = 0x0073;
    private const uint SPI_SETDESKWALLPAPER = 0x0014;

    private static readonly HashSet<string> IgnoredClasses = new(StringComparer.OrdinalIgnoreCase)
    {
        "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    };

    /// <summary>
    /// Returns the window that wallpaper windows should be parented to (the WorkerW
    /// that sits between the static wallpaper and the desktop icons), plus the
    /// SHELLDLL_DefView when running on the 24H2+ layout where they share Progman.
    /// </summary>
    /// <summary>False while Explorer is (re)starting and the desktop isn't back yet.</summary>
    public static bool DesktopReady() => FindWindow("Progman", null) != IntPtr.Zero;

    public static (IntPtr host, IntPtr defViewSibling) GetWallpaperHost()
    {
        IntPtr progman = FindWindow("Progman", null);
        // Ask Explorer to spawn the WorkerW layer behind the desktop icons.
        SendMessageTimeout(progman, 0x052C, IntPtr.Zero, IntPtr.Zero, SMTO_NORMAL, 1000, out _);
        SendMessageTimeout(progman, 0x052C, new IntPtr(0xD), new IntPtr(1), SMTO_NORMAL, 1000, out _);

        // Windows 11 24H2+: WorkerW is a child of Progman, alongside SHELLDLL_DefView.
        IntPtr childWorker = FindWindowEx(progman, IntPtr.Zero, "WorkerW", null);
        if (childWorker != IntPtr.Zero)
            return (progman, FindWindowEx(progman, IntPtr.Zero, "SHELLDLL_DefView", null));

        // Classic layout (23H2 and earlier): the WorkerW right after the one hosting the icons.
        IntPtr worker = IntPtr.Zero;
        EnumWindows((top, _) =>
        {
            if (FindWindowEx(top, IntPtr.Zero, "SHELLDLL_DefView", null) != IntPtr.Zero)
                worker = FindWindowEx(IntPtr.Zero, top, "WorkerW", null);
            return true;
        }, IntPtr.Zero);
        return (worker != IntPtr.Zero ? worker : progman, IntPtr.Zero);
    }

    /// <summary>Converts screen coordinates to the client coordinates of <paramref name="parent"/>.</summary>
    public static Rectangle ScreenToClient(IntPtr parent, Rectangle screen)
    {
        var r = new RECT { Left = screen.Left, Top = screen.Top, Right = screen.Right, Bottom = screen.Bottom };
        MapWindowPoints(IntPtr.Zero, parent, ref r, 2);
        return r.ToRectangle();
    }

    public static TimeSpan UserIdleTime()
    {
        var info = new LASTINPUTINFO { cbSize = (uint)Marshal.SizeOf<LASTINPUTINFO>() };
        if (!GetLastInputInfo(ref info)) return TimeSpan.Zero;
        return TimeSpan.FromMilliseconds(unchecked((uint)Environment.TickCount - info.dwTime));
    }

    /// <summary>
    /// The largest fraction of <paramref name="monitor"/> covered by any single real
    /// application window (ignores the shell, tool/overlay windows, minimized and
    /// cloaked windows, and this process's own windows).
    /// </summary>
    public static double LargestCoverFraction(Rectangle monitor, out string? coveringWindow)
    {
        uint ownPid = (uint)Environment.ProcessId;
        double best = 0;
        string? bestName = null;
        long monitorArea = (long)monitor.Width * monitor.Height;
        var cls = new StringBuilder(256);

        EnumWindows((hWnd, _) =>
        {
            if (!IsWindowVisible(hWnd) || IsIconic(hWnd)) return true;
            GetWindowThreadProcessId(hWnd, out uint pid);
            if (pid == ownPid) return true;

            if (DwmGetWindowAttribute(hWnd, DWMWA_CLOAKED, out int cloaked, sizeof(int)) == 0 && cloaked != 0)
                return true;

            long ex = GetWindowLongPtr(hWnd, GWL_EXSTYLE).ToInt64();
            if ((ex & (WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)) != 0) return true;

            cls.Clear();
            GetClassName(hWnd, cls, cls.Capacity);
            if (IgnoredClasses.Contains(cls.ToString())) return true;

            if (DwmGetWindowAttribute(hWnd, DWMWA_EXTENDED_FRAME_BOUNDS, out RECT r, Marshal.SizeOf<RECT>()) != 0)
                return true;
            var hit = Rectangle.Intersect(r.ToRectangle(), monitor);
            if (hit.IsEmpty) return true;

            double fraction = (double)hit.Width * hit.Height / monitorArea;
            if (fraction > best)
            {
                best = fraction;
                bestName = cls.ToString();
            }
            return true;
        }, IntPtr.Zero);

        coveringWindow = bestName;
        return best;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_POWER_THROTTLING_STATE
    {
        public uint Version, ControlMask, StateMask;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetProcessInformation(IntPtr process, int infoClass,
        ref PROCESS_POWER_THROTTLING_STATE info, int size);

    [DllImport("shell32.dll")]
    private static extern int SHQueryUserNotificationState(out int state);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    /// <summary>
    /// Puts a process in Windows "Efficiency mode" (what Task Manager's leaf button does):
    /// low priority plus EcoQoS, which keeps it on the efficiency cores at low clocks.
    /// </summary>
    public static bool SetEfficiencyMode(int pid)
    {
        try
        {
            using var p = System.Diagnostics.Process.GetProcessById(pid);
            p.PriorityClass = System.Diagnostics.ProcessPriorityClass.Idle;
            var state = new PROCESS_POWER_THROTTLING_STATE { Version = 1, ControlMask = 0x1, StateMask = 0x1 };
            return SetProcessInformation(p.Handle, 4 /* ProcessPowerThrottling */, ref state, Marshal.SizeOf(state));
        }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            return false; // process already exited or is protected
        }
    }

    /// <summary>True while a game or other app runs full-screen (exclusive or borderless).</summary>
    /// <param name="notGames">Programs that don't count when they're full-screen (browsers: someone
    /// watching a video, maybe this very camera, isn't gaming and shouldn't blind the tracker).</param>
    public static bool FullscreenAppRunning(ICollection<string>? notGames = null)
    {
#if DEBUG
        if (File.Exists(Path.Combine(AppContext.BaseDirectory, "simulate-fullscreen"))) return true; // for testing game mode
#endif
        // 1 = QUNS_NOT_PRESENT (locked / screen saver): nobody is gaming, keep the tracker running.
        // 2 = QUNS_BUSY (full-screen app), 3 = QUNS_RUNNING_D3D_FULL_SCREEN, 4 = QUNS_PRESENTATION_MODE
        if (SHQueryUserNotificationState(out int state) == 0)
        {
            if (state == 3) return true;  // exclusive full-screen Direct3D: a game
            if (state is 2 or 4) return !ForegroundIsOneOf(notGames);
            if (state == 1) return false;
        }

        // Fallback: the foreground window exactly covers its monitor (taskbar included).
        IntPtr fg = GetForegroundWindow();
        if (fg == IntPtr.Zero) return false;
        GetWindowThreadProcessId(fg, out uint pid);
        if (pid == (uint)Environment.ProcessId || ForegroundIsOneOf(notGames)) return false;
        var cls = new StringBuilder(64);
        GetClassName(fg, cls, cls.Capacity);
        // CoreWindow = Start menu, search, lock screen: full-screen shell surfaces, not apps.
        if (IgnoredClasses.Contains(cls.ToString()) || cls.ToString() == "Windows.UI.Core.CoreWindow") return false;
        if (DwmGetWindowAttribute(fg, DWMWA_EXTENDED_FRAME_BOUNDS, out RECT r, Marshal.SizeOf<RECT>()) != 0) return false;
        var rect = r.ToRectangle();
        return Screen.AllScreens.Any(s => s.Bounds == rect);
    }

    private static bool ForegroundIsOneOf(ICollection<string>? programs)
    {
        if (programs == null || programs.Count == 0) return false;
        IntPtr fg = GetForegroundWindow();
        if (fg == IntPtr.Zero) return false;
        GetWindowThreadProcessId(fg, out uint pid);
        try
        {
            using var p = Process.GetProcessById((int)pid);
            return programs.Contains(p.ProcessName, StringComparer.OrdinalIgnoreCase);
        }
        catch (ArgumentException)
        {
            return false;  // it just closed
        }
    }

    /// <summary>Re-applies the current static wallpaper so Explorer repaints the desktop.</summary>
    public static void RefreshStaticWallpaper()
    {
        var path = new StringBuilder(1024);
        if (SystemParametersInfo(SPI_GETDESKWALLPAPER, (uint)path.Capacity, path, 0) && path.Length > 0)
            SystemParametersInfo(SPI_SETDESKWALLPAPER, 0, path.ToString(), 0);
    }
}
