using System.Runtime.InteropServices;

namespace LiveCams;

/// <summary>Sets a normal (static) Windows wallpaper on one specific monitor.</summary>
internal static class StaticWallpaper
{
    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [ComImport, Guid("B92B56A9-8B55-4E14-9A89-0199BBB6F93B"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IDesktopWallpaper
    {
        void SetWallpaper([MarshalAs(UnmanagedType.LPWStr)] string? monitorId, [MarshalAs(UnmanagedType.LPWStr)] string wallpaper);
        [return: MarshalAs(UnmanagedType.LPWStr)] string GetWallpaper([MarshalAs(UnmanagedType.LPWStr)] string? monitorId);
        [return: MarshalAs(UnmanagedType.LPWStr)] string GetMonitorDevicePathAt(uint index);
        uint GetMonitorDevicePathCount();
        RECT GetMonitorRECT([MarshalAs(UnmanagedType.LPWStr)] string monitorId);
        void SetBackgroundColor(uint color);
        uint GetBackgroundColor();
        void SetPosition(int position);
        int GetPosition();
        // (slideshow methods follow in the real interface; unused here)
    }

    [ComImport, Guid("C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD")]
    private class DesktopWallpaperClass { }

    private const int DWPOS_FILL = 4;

    /// <summary>Shows <paramref name="imagePath"/> on the monitor whose bounds equal <paramref name="monitorBounds"/>.</summary>
    public static bool SetForMonitor(Rectangle monitorBounds, string imagePath)
    {
        var wallpaper = (IDesktopWallpaper)new DesktopWallpaperClass();
        try
        {
            uint count = wallpaper.GetMonitorDevicePathCount();
            for (uint i = 0; i < count; i++)
            {
                string id = wallpaper.GetMonitorDevicePathAt(i);
                RECT r = wallpaper.GetMonitorRECT(id);
                if (Rectangle.FromLTRB(r.Left, r.Top, r.Right, r.Bottom) != monitorBounds) continue;
                wallpaper.SetPosition(DWPOS_FILL);
                wallpaper.SetWallpaper(id, imagePath);
                return true;
            }
            return false;
        }
        finally
        {
            Marshal.ReleaseComObject(wallpaper);
        }
    }
}
