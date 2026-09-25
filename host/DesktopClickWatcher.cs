using System.Runtime.InteropServices;
using System.Text;

namespace LiveCams;

/// <summary>
/// Reports left-clicks on the desktop itself (the icon layer that sits above the
/// wallpaper), so clicking a cam's monitor can resume or refresh that cam.
/// The low-level hook lives on its own time-critical thread: every mouse event in
/// Windows passes through it, so it must answer instantly even when the PC is busy
/// and this process runs at idle priority.
/// </summary>
internal sealed class DesktopClickWatcher : IDisposable
{
    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X, Y; }

    [StructLayout(LayoutKind.Sequential)]
    private struct MSLLHOOKSTRUCT
    {
        public POINT pt;
        public uint mouseData, flags, time;
        public IntPtr extraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MSG
    {
        public IntPtr hwnd;
        public uint message;
        public IntPtr wParam, lParam;
        public uint time;
        public POINT pt;
    }

    private delegate IntPtr LowLevelMouseProc(int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook, LowLevelMouseProc fn, IntPtr hMod, uint threadId);

    [DllImport("user32.dll")]
    private static extern bool UnhookWindowsHookEx(IntPtr hook);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(IntPtr hook, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern int GetMessage(out MSG msg, IntPtr hWnd, uint min, uint max);

    [DllImport("user32.dll")]
    private static extern bool PostThreadMessage(uint threadId, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern IntPtr WindowFromPoint(POINT p);

    [DllImport("user32.dll")]
    private static extern IntPtr GetParent(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr hWnd, StringBuilder name, int max);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandle(string? name);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentThread();

    [DllImport("kernel32.dll")]
    private static extern bool SetThreadPriority(IntPtr thread, int priority);

    private const int WH_MOUSE_LL = 14;
    private const int WM_LBUTTONDOWN = 0x0201;
    private const uint WM_QUIT = 0x0012;
    private const int THREAD_PRIORITY_TIME_CRITICAL = 15;

    private readonly SynchronizationContext ui;
    private readonly Action<Point> onDesktopClick;
    private readonly LowLevelMouseProc proc; // held so the GC can't collect the callback
    private Thread? thread;
    private uint threadId;
    private IntPtr hook;

    public DesktopClickWatcher(Action<Point> onDesktopClick)
    {
        this.onDesktopClick = onDesktopClick;
        ui = SynchronizationContext.Current ?? new WindowsFormsSynchronizationContext();
        proc = HookProc;
        Enabled = true;
    }

    /// <summary>Installs or removes the hook (it is removed entirely while a game runs).</summary>
    public bool Enabled
    {
        get => thread != null;
        set
        {
            if (value == Enabled) return;
            if (value) Start();
            else Stop();
        }
    }

    private void Start()
    {
        using var ready = new ManualResetEventSlim();
        thread = new Thread(() =>
        {
            SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
            threadId = GetCurrentThreadId();
            hook = SetWindowsHookEx(WH_MOUSE_LL, proc, GetModuleHandle(null), 0);
            if (hook == IntPtr.Zero) Log.Write($"desktop click hook failed (error {Marshal.GetLastWin32Error()})");
            ready.Set();
            while (GetMessage(out _, IntPtr.Zero, 0, 0) > 0) { } // LL hooks are called from this loop
            if (hook != IntPtr.Zero) UnhookWindowsHookEx(hook);
            hook = IntPtr.Zero;
        })
        { IsBackground = true, Name = "DesktopClickHook" };
        thread.Start();
        ready.Wait();
    }

    private void Stop()
    {
        PostThreadMessage(threadId, WM_QUIT, IntPtr.Zero, IntPtr.Zero);
        thread!.Join(1000);
        thread = null;
    }

    private IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0 && wParam == WM_LBUTTONDOWN)
        {
            var pt = Marshal.PtrToStructure<MSLLHOOKSTRUCT>(lParam).pt;
            ui.Post(_ => Check(pt), null); // answer the hook immediately; do the work on the UI thread
        }
        return CallNextHookEx(hook, nCode, wParam, lParam);
    }

    private void Check(POINT pt)
    {
        IntPtr hit = WindowFromPoint(pt);
        if (ClassOf(hit) == "SysListView32" && ClassOf(GetParent(hit)) == "SHELLDLL_DefView")
            onDesktopClick(new Point(pt.X, pt.Y));
    }

    private static string ClassOf(IntPtr hWnd)
    {
        var sb = new StringBuilder(64);
        GetClassName(hWnd, sb, sb.Capacity);
        return sb.ToString();
    }

    public void Dispose() => Enabled = false;
}
