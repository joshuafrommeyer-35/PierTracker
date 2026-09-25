namespace LiveCams;

internal static class Log
{
    private const long MaxBytes = 1_000_000;
    private static readonly object Gate = new();
    private static string? path;

    public static void Init(string logDir)
    {
        Directory.CreateDirectory(logDir);
        path = Path.Combine(logDir, "host.log");
    }

    public static void Write(string message)
    {
        if (path == null) return;
        lock (Gate)
        {
            try
            {
                if (File.Exists(path) && new FileInfo(path).Length > MaxBytes)
                    File.Move(path, path + ".old", overwrite: true);
                File.AppendAllText(path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {message}{Environment.NewLine}");
            }
            catch (IOException)
            {
                // Logging must never take the wallpaper down.
            }
        }
    }
}
