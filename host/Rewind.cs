using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace LiveCams;

/// <summary>
/// A cam's video on disk, so it can be rewound. The player streams it as 10-second MPEG-TS pieces;
/// <see cref="RewindRecorder"/> saves each piece as it downloads (nothing is re-encoded and no second
/// stream is opened), named by when it's on screen, in rewind/&lt;date&gt;/. <see cref="RewindForm"/>
/// plays any moment back by the clock. Clips and pictures a person keeps go to saved/, which nothing
/// ever deletes. Local only, like the frames: nothing here is uploaded.
/// </summary>
internal sealed class VideoStore
{
    public const string Host = "livecams.video"; // what the rewind window's page is served as

    // <start yyyyMMdd-HHmmss-fff>_<duration ms>_<the stream's sequence number>.ts
    private static readonly Regex PieceName = new(@"^(\d{8}-\d{6}-\d{3})_(\d+)_(\d+)\.ts$", RegexOptions.Compiled);

    public VideoStore(string root)
    {
        Root = Path.GetFullPath(root);
        RewindDir = Path.Combine(Root, "rewind");
        SavedDir = Path.Combine(Root, "saved");
    }

    public string Root { get; }
    public string RewindDir { get; }
    public string SavedDir { get; }

    public sealed record Piece(string Path, DateTime Start, TimeSpan Duration, long Sequence, long Bytes)
    {
        public DateTime End => Start + Duration;
    }

    public static string FileName(DateTime start, TimeSpan duration, long sequence) =>
        $"{start:yyyyMMdd-HHmmss-fff}_{(long)duration.TotalMilliseconds}_{sequence}.ts";

    /// <summary>Every piece kept, oldest first.</summary>
    public List<Piece> Pieces()
    {
        var pieces = new List<Piece>();
        if (!Directory.Exists(RewindDir)) return pieces;
        foreach (string file in Directory.EnumerateFiles(RewindDir, "*.ts", SearchOption.AllDirectories))
        {
            var m = PieceName.Match(Path.GetFileName(file));
            if (!m.Success || !DateTime.TryParseExact(m.Groups[1].Value, "yyyyMMdd-HHmmss-fff",
                    CultureInfo.InvariantCulture, DateTimeStyles.None, out var start))
                continue;
            long bytes;
            try { bytes = new FileInfo(file).Length; }
            catch (IOException) { continue; } // deleted meanwhile
            pieces.Add(new Piece(file, start, TimeSpan.FromMilliseconds(long.Parse(m.Groups[2].Value)),
                long.Parse(m.Groups[3].Value), bytes));
        }
        pieces.Sort((a, b) => a.Start.CompareTo(b.Start));
        return pieces;
    }

    /// <summary>
    /// rewind.m3u8: all the kept video as one playlist, each piece with its clock time. A break (night,
    /// a game, a reload) starts a new stretch.
    /// </summary>
    public int WritePlaylist()
    {
        var pieces = Pieces();
        var text = new StringBuilder();
        int target = pieces.Count == 0 ? 10 : (int)Math.Ceiling(pieces.Max(p => p.Duration.TotalSeconds));
        text.Append($"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-TARGETDURATION:{target}\n#EXT-X-MEDIA-SEQUENCE:0\n");
        Piece? previous = null;
        foreach (var p in pieces)
        {
            bool joined = previous != null && p.Sequence == previous.Sequence + 1
                          && Math.Abs((p.Start - previous.End).TotalSeconds) < 1.5;
            if (previous != null && !joined) text.Append("#EXT-X-DISCONTINUITY\n");
            text.Append("#EXT-X-PROGRAM-DATE-TIME:")
                .Append(new DateTimeOffset(p.Start).ToString("yyyy-MM-ddTHH:mm:ss.fffzzz", CultureInfo.InvariantCulture))
                .Append('\n')
                .Append(string.Create(CultureInfo.InvariantCulture, $"#EXTINF:{p.Duration.TotalSeconds:0.000},\n"))
                .Append(Path.GetRelativePath(Root, p.Path).Replace('\\', '/')).Append('\n');
            previous = p;
        }
        text.Append("#EXT-X-ENDLIST\n");
        Directory.CreateDirectory(Root);
        File.WriteAllText(Path.Combine(Root, "rewind.m3u8"), text.ToString());
        return pieces.Count;
    }

    /// <summary>The rewind window's page and its player (hls.js), next to the video they play.</summary>
    public void InstallViewer()
    {
        string from = Path.Combine(AppContext.BaseDirectory, "viewer"), to = Path.Combine(Root, "viewer");
        Directory.CreateDirectory(to);
        foreach (string file in Directory.EnumerateFiles(from))
            File.Copy(file, Path.Combine(to, Path.GetFileName(file)), overwrite: true);
    }

    /// <summary>
    /// Keeps the video from <paramref name="from"/> to <paramref name="to"/> (whole pieces, so up to 10 s
    /// more at each end) in saved/, as an .mp4 when ffmpeg is installed, else as a .ts that VLC and
    /// Windows Media Player play. Returns the file, or null if there's no video from then.
    /// </summary>
    public async Task<string?> SaveClipAsync(DateTime from, DateTime to)
    {
        var parts = Pieces().Where(p => p.End > from && p.Start < to).ToList();
        if (parts.Count == 0) return null;
        Directory.CreateDirectory(SavedDir);
        string ts = Path.Combine(SavedDir, $"underwater {parts[0].Start:yyyy-MM-dd HH.mm.ss}.ts");
        await using (var output = File.Create(ts))
        {
            foreach (var p in parts)
            {
                await using var input = File.OpenRead(p.Path); // MPEG-TS pieces of one stream join end to end
                await input.CopyToAsync(output);
            }
        }
        if (FindOnPath("ffmpeg.exe") is not { } ffmpeg) return ts;

        string mp4 = Path.ChangeExtension(ts, ".mp4");
        var start = new ProcessStartInfo(ffmpeg) { CreateNoWindow = true, UseShellExecute = false, RedirectStandardError = true };
        foreach (string arg in new[] { "-hide_banner", "-loglevel", "error", "-y", "-i", ts, "-c", "copy", "-movflags", "+faststart", mp4 })
            start.ArgumentList.Add(arg);
        try
        {
            using var ff = Process.Start(start)!;
            string errors = await ff.StandardError.ReadToEndAsync();
            await ff.WaitForExitAsync();
            if (ff.ExitCode == 0 && File.Exists(mp4))
            {
                File.Delete(ts);
                return mp4;
            }
            Log.Write($"rewind: ffmpeg couldn't make an .mp4 ({errors.Trim()}); kept the .ts");
        }
        catch (System.ComponentModel.Win32Exception ex)
        {
            Log.Write($"rewind: couldn't run ffmpeg ({ex.Message}); kept the .ts");
        }
        return ts;
    }

    public string SaveStill(byte[] jpeg, DateTime at)
    {
        Directory.CreateDirectory(SavedDir);
        string path = Path.Combine(SavedDir, $"underwater {at:yyyy-MM-dd HH.mm.ss}.jpg");
        File.WriteAllBytes(path, jpeg);
        return path;
    }

    private static string? FindOnPath(string exe) =>
        (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries)
            .Select(dir => Path.Combine(dir.Trim(), exe)).FirstOrDefault(File.Exists);
}

/// <summary>
/// Saves a cam's video pieces as its player downloads them (see <see cref="VideoStore"/>), and deletes
/// the oldest once the folder passes its size limit or the drive runs low. Night video is skipped.
/// </summary>
internal sealed class RewindRecorder
{
    private readonly VideoStore store;
    private readonly RewindConfig config;
    private readonly string trackerVerdict; // the tracker's view of the latest frame (live.json): dark or not
    private readonly Func<DateTime?> onScreenAt;
    private readonly Dictionary<string, double> durations = new(); // piece file name -> seconds, from the stream's playlist
    private readonly LinkedList<(string Path, long Bytes)> kept = new(); // oldest first
    private long keptBytes;
    private long lastSequence = -1;
    private DateTime nextStart;
    private int disagreements;
    private DateTime lastProblemLogged;

    /// <param name="onScreenAt">When the next piece the player adds will be on screen, from the player's
    /// buffer (null if unknown). The player shows the stream ~20-30 s after downloading it.</param>
    public RewindRecorder(RewindConfig config, string configDir, string? captureDir, Func<DateTime?> onScreenAt)
    {
        this.config = config;
        this.onScreenAt = onScreenAt;
        store = new VideoStore(Path.Combine(configDir, config.Dir));
        trackerVerdict = Path.Combine(captureDir ?? Path.Combine(configDir, "frames", "underwater"), "live.json");
        foreach (var p in store.Pieces())
        {
            kept.AddLast((p.Path, p.Bytes));
            keptBytes += p.Bytes;
        }
    }

    public VideoStore Store => store;

    public void Attach(CoreWebView2 core) => core.WebResourceResponseReceived += (_, e) => _ = OnResponseAsync(e);

    /// <summary>Forget the stream's position (the page reloaded or unloaded): the next piece starts a new stretch.</summary>
    public void Break() => lastSequence = -1;

    private async Task OnResponseAsync(CoreWebView2WebResourceResponseReceivedEventArgs e)
    {
        string path;
        try { path = new Uri(e.Request.Uri).AbsolutePath; }
        catch (UriFormatException) { return; }
        bool playlist = path.EndsWith(".m3u8", StringComparison.OrdinalIgnoreCase);
        bool piece = path.EndsWith(".ts", StringComparison.OrdinalIgnoreCase);
        if ((!playlist && !piece) || e.Response.StatusCode != 200) return;
        DateTime arrived = DateTime.Now;
        try
        {
            using var body = await e.Response.GetContentAsync();
            if (body == null) return;
            if (playlist)
                ReadDurations(body);
            else
                await SaveAsync(body, Path.GetFileName(path), arrived);
        }
        catch (Exception ex) when (ex is COMException or IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            // Happens when the page reloads mid-download. Logged now and then, not every time.
            if (DateTime.UtcNow - lastProblemLogged > TimeSpan.FromMinutes(10))
            {
                lastProblemLogged = DateTime.UtcNow;
                Log.Write($"rewind: a piece wasn't saved ({ex.GetType().Name}: {ex.Message})");
            }
        }
    }

    /// <summary>The stream's playlist lists each piece with its length ("#EXTINF:10.0," then its address).</summary>
    private void ReadDurations(Stream body)
    {
        using var reader = new StreamReader(body);
        double? pending = null;
        if (durations.Count > 100) durations.Clear();
        for (string? line = reader.ReadLine(); line != null; line = reader.ReadLine())
        {
            line = line.Trim();
            if (line.StartsWith("#EXTINF:", StringComparison.Ordinal))
            {
                string value = line[8..].Split(',')[0];
                pending = double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out double s) ? s : null;
            }
            else if (pending is { } seconds && line.Length > 0 && !line.StartsWith('#'))
            {
                string name = line.Split('?')[0];
                durations[name[(name.LastIndexOf('/') + 1)..]] = seconds;
                pending = null;
            }
        }
    }

    private async Task SaveAsync(Stream body, string name, DateTime arrived)
    {
        if (config.SkipDark && IsNight())
        {
            lastSequence = -1;
            return;
        }
        long sequence = SequenceOf(name);
        double seconds = durations.TryGetValue(name, out double d) ? d : 10.0;
        DateTime start = OnScreenFrom(sequence, arrived);
        nextStart = start.AddSeconds(seconds);
        lastSequence = sequence;

        string dir = Path.Combine(store.RewindDir, start.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));
        Directory.CreateDirectory(dir);
        string file = Path.Combine(dir, VideoStore.FileName(start, TimeSpan.FromSeconds(seconds), sequence));
        string partial = file + ".partial";
        await using (var output = File.Create(partial))
            await body.CopyToAsync(output);
        File.Move(partial, file, overwrite: true);
        long bytes = new FileInfo(file).Length;
        kept.AddLast((file, bytes));
        keptBytes += bytes;
        Prune();
    }

    /// <summary>
    /// When a piece is on screen. One that follows the last starts where that one ends; the first
    /// after a break starts when the player's buffer says, or on arrival before the player has said
    /// (it starts playing about a second later). The player's measure wins when it disagrees by more
    /// than 0.75 s twice in a row: that corrects the first guess, and follows stalls and catch-ups.
    /// </summary>
    private DateTime OnScreenFrom(long sequence, DateTime arrived)
    {
        DateTime? measured = onScreenAt();
        if (lastSequence >= 0 && sequence == lastSequence + 1)
        {
            if (measured is { } m && Math.Abs((m - nextStart).TotalSeconds) > 0.75)
            {
                if (++disagreements >= 2)
                {
                    disagreements = 0;
                    return m;
                }
            }
            else
            {
                disagreements = 0;
            }
            return nextStart;
        }
        disagreements = 0;
        return measured ?? arrived; // a fresh start plays from the first piece it downloads
    }

    private static long SequenceOf(string name)
    {
        var m = Regex.Match(name, @"(\d+)\.ts$", RegexOptions.IgnoreCase);
        return m.Success && long.TryParse(m.Groups[1].Value, out long n) ? n : 0;
    }

    private bool IsNight()
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(trackerVerdict));
            var root = doc.RootElement;
            return root.GetProperty("status").GetString() == "dark"
                   && DateTime.TryParse(root.GetProperty("taken_at").GetString(), CultureInfo.InvariantCulture,
                       DateTimeStyles.None, out var at)
                   && DateTime.Now - at < TimeSpan.FromMinutes(2);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException
                                       or KeyNotFoundException or InvalidOperationException)
        {
            return false; // no tracker verdict: keep the video
        }
    }

    /// <summary>Oldest first, until the folder is within its limit and the drive has its free space back.</summary>
    private void Prune()
    {
        long max = (long)(config.MaxGB * 1e9), minFree = (long)(config.MinFreeGB * 1e9);
        long free;
        try { free = new DriveInfo(Path.GetPathRoot(store.RewindDir)!).AvailableFreeSpace; }
        catch (Exception ex) when (ex is IOException or ArgumentException or UnauthorizedAccessException) { free = long.MaxValue; }
        bool lowDisk = free < minFree;
        while (kept.First is { } oldest && (keptBytes > max || free < minFree))
        {
            try { File.Delete(oldest.Value.Path); }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { break; } // being played back right now
            kept.RemoveFirst();
            keptBytes -= oldest.Value.Bytes;
            free += oldest.Value.Bytes;
            string dir = Path.GetDirectoryName(oldest.Value.Path)!;
            try
            {
                if (!Directory.EnumerateFileSystemEntries(dir).Any()) Directory.Delete(dir);
            }
            catch (IOException) { } // a new piece landed in it meanwhile
        }
        if (lowDisk && DateTime.UtcNow - lastProblemLogged > TimeSpan.FromMinutes(10))
        {
            lastProblemLogged = DateTime.UtcNow;
            Log.Write($"rewind: drive below {config.MinFreeGB} GB free; oldest video deleted ({keptBytes / 1e9:0.0} GB kept)");
        }
    }
}

/// <summary>
/// The rewind window: plays back the kept video by the clock, and keeps clips and pictures. The page
/// (viewer/rewind.html) runs in its own browser profile at normal priority, unlike the cams.
/// </summary>
internal sealed class RewindForm : Form
{
    private readonly string configDir;
    private readonly VideoStore store;
    private readonly WebView2 web = new() { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(15, 19, 24) };
    private DateTime? openAt;
    private bool ready;

    public RewindForm(string configDir, VideoStore store, DateTime? openAt = null)
    {
        this.configDir = configDir;
        this.store = store;
        this.openAt = openAt;
        Text = "Underwater cam · rewind";
        StartPosition = FormStartPosition.CenterScreen;
        Size = new Size(1500, 950);
        MinimumSize = new Size(900, 600);
        BackColor = Color.FromArgb(15, 19, 24);
        Controls.Add(web);
        Load += async (_, _) => await InitAsync();
    }

    /// <summary>Jumps to a moment (e.g. "I just saw something": a minute ago), refreshing the video list.</summary>
    public void ShowMoment(DateTime at)
    {
        if (!ready)
        {
            openAt = at;
            return;
        }
        store.WritePlaylist();
        Post(new { type = "reload", at = Millis(at) });
    }

    private async Task InitAsync()
    {
        try
        {
            var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(configDir, "host", "webview-viewer"));
            await web.EnsureCoreWebView2Async(env);
            var core = web.CoreWebView2;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            store.InstallViewer();
            store.WritePlaylist();
            core.SetVirtualHostNameToFolderMapping(VideoStore.Host, store.Root, CoreWebView2HostResourceAccessKind.Allow);
            core.WebMessageReceived += async (_, e) => await OnMessageAsync(e.WebMessageAsJson);
            core.NavigationCompleted += (_, _) => ready = true;
            string at = openAt is { } t ? $"?at={Millis(t)}" : "";
            core.Navigate($"https://{VideoStore.Host}/viewer/rewind.html{at}");
        }
        catch (Exception ex)
        {
            Log.Write($"rewind window failed: {ex}");
            MessageBox.Show(this, ex.Message, "LiveCams rewind", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }

    private async Task OnMessageAsync(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var m = doc.RootElement;
            switch (m.GetProperty("type").GetString())
            {
                case "keep":
                {
                    var at = FromMillis(m.GetProperty("at").GetDouble());
                    Post(new { type = "toast", text = "Keeping 2 minutes before and 1 minute after..." });
                    string? clip = await store.SaveClipAsync(at.AddMinutes(-2), at.AddMinutes(1));
                    Post(new { type = "toast", text = clip == null ? "There's no video from then." : $"Kept: saved\\{Path.GetFileName(clip)}" });
                    Log.Write($"rewind: kept {(clip == null ? "nothing" : Path.GetFileName(clip))}");
                    break;
                }
                case "still":
                {
                    string data = m.GetProperty("data").GetString() ?? "";
                    int comma = data.IndexOf(',');
                    if (comma < 0) break;
                    string still = store.SaveStill(Convert.FromBase64String(data[(comma + 1)..]), FromMillis(m.GetProperty("at").GetDouble()));
                    Post(new { type = "toast", text = $"Saved: saved\\{Path.GetFileName(still)}" });
                    break;
                }
                case "saved":
                    Directory.CreateDirectory(store.SavedDir);
                    Process.Start("explorer.exe", store.SavedDir);
                    break;
                case "refresh":
                    store.WritePlaylist();
                    Post(new { type = "reload", at = m.TryGetProperty("at", out var a) && a.ValueKind == JsonValueKind.Number ? a.GetDouble() : (double?)null });
                    break;
            }
        }
        catch (Exception ex) when (ex is JsonException or IOException or UnauthorizedAccessException or FormatException
                                       or KeyNotFoundException or InvalidOperationException)
        {
            Log.Write($"rewind window: {ex.Message}");
            Post(new { type = "toast", text = "That didn't work: " + ex.Message });
        }
    }

    private void Post(object message)
    {
        if (web.CoreWebView2 != null) web.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(message));
    }

    private static long Millis(DateTime local) => new DateTimeOffset(local).ToUnixTimeMilliseconds();

    private static DateTime FromMillis(double ms) => DateTimeOffset.FromUnixTimeMilliseconds((long)ms).LocalDateTime;
}
