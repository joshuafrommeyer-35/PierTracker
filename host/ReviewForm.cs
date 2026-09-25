using System.Text;
using System.Text.Json;

namespace LiveCams;

/// <summary>
/// Walks through data/review/pending: sightings the tracker wasn't sure about ("uncertain"),
/// and a sample of names it did log ("check"). Approve a guess, pick the right animal, or
/// mark it "not an animal". Decisions go to data/review/decisions.csv; answers to checks
/// become the published accuracy, approved uncertain ones the "Confirmed by hand" list.
/// Keys: 1-3 pick a guess, N = not an animal, S or Right = skip, C = copy the picture
/// (to paste into a chat or iNaturalist when you want help with an ID; skip it meanwhile).
/// </summary>
internal sealed class ReviewForm : Form
{
    private sealed record Guess(string Common, string Scientific, string Category, double Prob);
    private sealed record Species(string Common, string Scientific, string Category, bool IsGroup = false)
    {
        public override string ToString() => IsGroup ? $"group: {Common}" : Common;
    }

    private readonly string pendingDir, approvedDir, rejectedDir, decisionsCsv;
    private readonly List<Species> species;
    private readonly PictureBox picture = new() { Dock = DockStyle.Fill, SizeMode = PictureBoxSizeMode.Zoom, BackColor = Color.FromArgb(24, 28, 34) };
    private readonly Label info = new() { Dock = DockStyle.Top, Height = 32, TextAlign = ContentAlignment.MiddleLeft, Padding = new Padding(8, 0, 0, 0) };
    private readonly Label suggestion = new() { Dock = DockStyle.Top, Height = 26, TextAlign = ContentAlignment.MiddleLeft,
                                                Padding = new Padding(8, 0, 0, 0), ForeColor = Color.SteelBlue };
    private readonly FlowLayoutPanel guessRow = new() { Dock = DockStyle.Top, Height = 44, Padding = new Padding(4) };
    private readonly ComboBox other = new() { Width = 300, DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly Label progress = new() { Dock = DockStyle.Bottom, Height = 26, ForeColor = Color.DimGray,
                                             TextAlign = ContentAlignment.MiddleLeft, Padding = new Padding(8, 0, 0, 0) };
    private List<string> items = new();
    private List<Guess> guesses = new();
    private string kind = "uncertain", loggedAs = "";
    private int index;

    public ReviewForm(string configDir)
    {
        string review = Path.Combine(configDir, "data", "review");
        pendingDir = Path.Combine(review, "pending");
        approvedDir = Path.Combine(review, "approved");
        rejectedDir = Path.Combine(review, "rejected");
        decisionsCsv = Path.Combine(review, "decisions.csv");
        species = LoadSpecies(Path.Combine(configDir, "tracker", "species.json"));

        Text = "Review uncertain sightings";
        Size = new Size(1060, 700);
        StartPosition = FormStartPosition.CenterScreen;
        KeyPreview = true;
        Font = new Font("Segoe UI", 10);

        other.Items.AddRange(species.Cast<object>().ToArray());
        progress.Text = TrainingProgress();
        var bottom = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 48, Padding = new Padding(4) };
        bottom.Controls.Add(new Label { Text = "Something else:", AutoSize = true, Margin = new Padding(6, 12, 0, 0) });
        bottom.Controls.Add(other);
        bottom.Controls.Add(MakeButton("Approve as this", () =>
        {
            if (other.SelectedIndex >= 0) Approve(species[other.SelectedIndex]);
        }));
        bottom.Controls.Add(MakeButton("Not an animal  (N)", Reject));
        bottom.Controls.Add(MakeButton("Skip  (S)", () => ShowItem(index + 1)));
        bottom.Controls.Add(MakeButton("Copy picture  (C)", CopyPicture));

        Controls.Add(picture);
        Controls.Add(guessRow);
        Controls.Add(suggestion);
        Controls.Add(info);
        Controls.Add(bottom);
        Controls.Add(progress);

        KeyDown += (_, e) =>
        {
            if (other.Focused) return; // typing in the list jumps to an animal; it isn't a shortcut
            if (e.KeyCode is >= Keys.D1 and <= Keys.D3 && e.KeyCode - Keys.D1 < guesses.Count)
                Approve(guesses[e.KeyCode - Keys.D1]);
            else if (e.KeyCode == Keys.N) Reject();
            else if (e.KeyCode is Keys.S or Keys.Right) ShowItem(index + 1);
            else if (e.KeyCode == Keys.C) CopyPicture();
            else return;
            e.Handled = true;
        };

        items = Directory.Exists(pendingDir)
            ? Directory.GetFiles(pendingDir, "*.json").OrderBy(f => f).ToList()
            : new List<string>();
        ShowItem(0);
    }

    public static int PendingCount(string configDir)
    {
        string dir = Path.Combine(configDir, "data", "review", "pending");
        return Directory.Exists(dir) ? Directory.GetFiles(dir, "*.json").Length : 0;
    }

    private Button MakeButton(string text, Action onClick)
    {
        var b = new Button { Text = text, AutoSize = true, Height = 34, Margin = new Padding(6, 4, 0, 0) };
        b.Click += (_, _) => onClick();
        return b;
    }

    private void ShowItem(int i)
    {
        picture.Image?.Dispose();
        picture.Image = null;
        foreach (var old in guessRow.Controls.Cast<Control>().ToList()) old.Dispose();
        index = i;
        if (index >= items.Count)
        {
            info.Text = items.Count == 0 ? "Nothing to review." : "All done. Close this window.";
            suggestion.Text = "";
            guesses = new();
            return;
        }

        string json = items[index];
        using var doc = JsonDocument.Parse(File.ReadAllText(json));
        guesses = doc.RootElement.GetProperty("guesses").EnumerateArray()
            .Select(g => new Guess(g.GetProperty("common").GetString()!, g.GetProperty("scientific").GetString()!,
                g.GetProperty("category").GetString()!, g.GetProperty("prob").GetDouble()))
            .ToList();
        string takenAt = doc.RootElement.GetProperty("taken_at").GetString()!;
        kind = doc.RootElement.TryGetProperty("kind", out var k) ? k.GetString()! : "uncertain";
        loggedAs = doc.RootElement.TryGetProperty("logged_as", out var l) ? l.GetString()! : "";
        // A second opinion attached to the card (e.g. by Claude): shown, never applied on its own.
        suggestion.Text = doc.RootElement.TryGetProperty("suggestion", out var sug) && sug.ValueKind == JsonValueKind.Object
            ? $"Suggestion: {sug.GetProperty("common").GetString()}" +
              (sug.TryGetProperty("why", out var why) ? $"  ({why.GetString()})" : "")
            : "";

        string jpg = Path.ChangeExtension(json, ".jpg");
        if (File.Exists(jpg))
            picture.Image = Image.FromStream(new MemoryStream(File.ReadAllBytes(jpg))); // don't lock the file

        string question = kind == "check"
            ? $"The tracker logged this as a {loggedAs}. Is that right?"
            : "The tracker wasn't sure. Is it one of these?";
        info.Text = $"{index + 1} of {items.Count}   |   {takenAt.Replace('T', ' ')}   |   {question}";
        for (int g = 0; g < guesses.Count; g++)
        {
            var guess = guesses[g];
            guessRow.Controls.Add(MakeButton($"{g + 1}. {guess.Common}  ({guess.Prob:P0})", () => Approve(guess)));
        }
    }

    private void CopyPicture()
    {
        if (picture.Image != null) Clipboard.SetImage(picture.Image);
    }

    private void Approve(Guess g) => Decide("approved", g.Common, g.Scientific, g.Category);

    private void Approve(Species s) => Decide("approved", s.Common, s.Scientific, s.Category);

    private void Reject() => Decide("rejected", "", "", "");

    private void Decide(string decision, string common, string scientific, string category)
    {
        if (index >= items.Count) return;
        string json = items[index];
        string jpg = Path.ChangeExtension(json, ".jpg");
        string destDir = decision == "approved" ? approvedDir : rejectedDir;
        Directory.CreateDirectory(destDir);

        using (var doc = JsonDocument.Parse(File.ReadAllText(json)))
        {
            string takenAt = doc.RootElement.GetProperty("taken_at").GetString()!;
            var best = guesses.FirstOrDefault();
            bool newFile = !File.Exists(decisionsCsv);
            var row = new[]
            {
                DateTime.Now.ToString("s"), takenAt, Path.GetFileName(jpg), kind, loggedAs, decision, common, scientific,
                category, best?.Common ?? "", best?.Prob.ToString("0.000") ?? "", "person",
            };
            var sb = new StringBuilder();
            if (newFile)
                sb.AppendLine("reviewed_at,taken_at,image,kind,logged_as,decision,common_name,scientific_name,category,best_guess,best_guess_prob,reviewer");
            sb.AppendLine(string.Join(",", row.Select(v => $"\"{v.Replace("\"", "\"\"")}\"")));
            File.AppendAllText(decisionsCsv, sb.ToString());
        }

        picture.Image?.Dispose();
        picture.Image = null;
        File.Move(json, Path.Combine(destDir, Path.GetFileName(json)), overwrite: true);
        if (File.Exists(jpg)) File.Move(jpg, Path.Combine(destDir, Path.GetFileName(jpg)), overwrite: true);
        items.RemoveAt(index);
        progress.Text = TrainingProgress();
        ShowItem(index);
    }

    /// <summary>Every animal in species.json, then its look-alike groups ("group: silversides &amp; sardines"),
    /// for when the kind of fish is clear but the exact species isn't.</summary>
    private static List<Species> LoadSpecies(string path)
    {
        if (!File.Exists(path)) return new();
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var all = doc.RootElement.GetProperty("species").EnumerateArray().ToList();
        var animals = all
            .Select(s => new Species(s.GetProperty("common").GetString()!, s.GetProperty("scientific").GetString()!,
                s.GetProperty("category").GetString()!))
            .OrderBy(s => s.Common, StringComparer.OrdinalIgnoreCase);
        var groups = all
            .Where(s => s.TryGetProperty("group", out _))
            .GroupBy(s => s.GetProperty("group").GetString()!)
            .Select(g => new Species(g.Key, "", g.First().GetProperty("category").GetString()!, IsGroup: true))
            .OrderBy(g => g.Common, StringComparer.OrdinalIgnoreCase);
        return animals.Concat(groups).ToList();
    }

    /// <summary>How many answers each animal has so far: what the camera-trained classifier learns from.</summary>
    private string TrainingProgress()
    {
        if (!File.Exists(decisionsCsv)) return "No answers yet.";
        var counts = new Dictionary<string, int>();
        foreach (var line in File.ReadLines(decisionsCsv).Skip(1))
        {
            var cells = line.Trim('"').Split("\",\"");  // every field is quoted (see Decide)
            if (cells.Length < 7) continue;
            string label = cells[5] == "approved" ? cells[6] : "not an animal";
            counts[label] = counts.GetValueOrDefault(label) + 1;
        }
        var top = counts.OrderByDescending(kv => kv.Value).Take(5).Select(kv => $"{kv.Key} {kv.Value}");
        return $"Answers so far: {counts.Values.Sum()} ({string.Join(", ", top)}). The camera-trained " +
               "classifier learns an animal at 12 answers; ~30 makes it reliable.";
    }
}
