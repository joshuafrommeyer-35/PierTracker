using System.Text.Json;

namespace LiveCams;

internal sealed class CamConfig
{
    public string Name { get; set; } = "";

    /// <summary>1-based monitor number, counted left to right.</summary>
    public int Monitor { get; set; } = 1;

    /// <summary>The official page that embeds the camera player.</summary>
    public string Page { get; set; } = "";

    /// <summary>CSS selector for the player iframe on <see cref="Page"/>.</summary>
    public string Iframe { get; set; } = "iframe";

    /// <summary>Extra height given to the player so a toolbar under the video falls off-screen.</summary>
    public int ExtraBottomPx { get; set; }

    /// <summary>
    /// "alwaysOn": keep the stream playing (restart it if it stalls).
    /// "resumeWhenWatched": let the player's own idle pause happen, and only resume it
    /// while this monitor's wallpaper is visible and the user is at the PC.
    /// </summary>
    public string Mode { get; set; } = "alwaysOn";

    /// <summary>
    /// If &gt; 0, the on-screen picture freezes after this many seconds live; clicking the
    /// desktop on this monitor goes live again. The stream itself keeps playing (for capture).
    /// </summary>
    public int FreezeDisplayAfterSeconds { get; set; }

    /// <summary>If &gt; 0, save a JPEG of the video this often (seconds) for the animal logger.</summary>
    public int CaptureEverySeconds { get; set; }

    /// <summary>Folder for captured frames, relative to the config file.</summary>
    public string? CaptureDir { get; set; }

    public bool ResumeWhenWatched => Mode.Equals("resumeWhenWatched", StringComparison.OrdinalIgnoreCase);
}

internal sealed class AppConfig
{
    public List<CamConfig> Cams { get; set; } = new();

    /// <summary>The user counts as "at the PC" if there was keyboard/mouse input within this many seconds.</summary>
    public int WatchedIdleSeconds { get; set; } = 120;

    /// <summary>A monitor counts as covered when one window fills at least this fraction of it.</summary>
    public double CoverThreshold { get; set; } = 0.5;

    /// <summary>Unload the cams (leaving a still) while a game or other app runs full-screen.</summary>
    public bool PauseDuringFullscreenApps { get; set; } = true;

    public TrackerConfig Tracker { get; set; } = new();

    /// <summary>If &gt; 0, expose Chrome DevTools on this localhost port (troubleshooting only).</summary>
    public int DebugPort { get; set; }

    public static string Locate()
    {
        // Walk up from the exe so the same config works for bin/ builds and the published app/ folder.
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir != null; dir = dir.Parent)
        {
            string candidate = Path.Combine(dir.FullName, "livecams.json");
            if (File.Exists(candidate)) return candidate;
        }
        throw new FileNotFoundException("livecams.json not found in the app folder or any parent folder.");
    }

    public static AppConfig Load(string path)
    {
        var options = new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
            ReadCommentHandling = JsonCommentHandling.Skip,
            AllowTrailingCommas = true,
        };
        return JsonSerializer.Deserialize<AppConfig>(File.ReadAllText(path), options)
            ?? throw new InvalidDataException("livecams.json is empty.");
    }
}

internal sealed class TrackerConfig
{
    public bool Enabled { get; set; }

    /// <summary>Python interpreter (pythonw.exe = no console window), relative to the config file.</summary>
    public string Python { get; set; } = "tracker/.venv/Scripts/pythonw.exe";

    public string Script { get; set; } = "tracker/tracker.py";

    /// <summary>Once a day, push summary stats to the project's GitHub README (tracker/publish_results.py).</summary>
    public bool PublishResults { get; set; }

    /// <summary>Once a day, copy the database, review answers, CSVs and frame bank here (e.g. Google Drive).</summary>
    public string? BackupDir { get; set; }
}
