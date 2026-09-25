using System.Text;
using System.Text.Json;

namespace LiveCams;

/// <summary>
/// Walks through the sightings the tracker wasn't sure about (data/review/pending):
/// approve one of its guesses, pick the right animal, or mark it "not an animal".
/// Decisions go to data/review/decisions.csv and feed the public results.
/// Keys: 1-3 pick a guess, N = not an animal, S or Right = skip.
/// </summary>
internal sealed class ReviewForm : Form
{
    private sealed record Guess(string Common, string Scientific, string Category, double Prob);
    private sealed record Species(string Common, string Scientific, string Category);

    private readonly string pendingDir, approvedDir, rejectedDir, decisionsCsv;
    private readonly List<Species> species;
    private readonly PictureBox picture = new() { Dock = DockStyle.Fill, SizeMode = PictureBoxSizeMode.Zoom, BackColor = Color.FromArgb(24, 28, 34) };
    private readonly Label info = new() { Dock = DockStyle.Top, Height = 28, TextAlign = ContentAlignment.MiddleLeft, Padding = new Padding(8, 0, 0, 0) };
    private readonly FlowLayoutPanel guessRow = new() { Dock = DockStyle.Top, Height = 44, Padding = new Padding(4) };
    private readonly ComboBox other = new() { Width = 260, DropDownStyle = ComboBoxStyle.DropDownList };
    private List<string> items = new();
    private List<Guess> guesses = new();
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

        other.Items.AddRange(species.Select(s => (object)s.Common).ToArray());
        var bottom = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 48, Padding = new Padding(4) };
        bottom.Controls.Add(new Label { Text = "Something else:", AutoSize = true, Margin = new Padding(6, 12, 0, 0) });
        bottom.Controls.Add(other);
        bottom.Controls.Add(MakeButton("Approve as this", () =>
        {
            if (other.SelectedIndex >= 0) Approve(species[other.SelectedIndex]);
        }));
        bottom.Controls.Add(MakeButton("Not an animal  (N)", Reject));
        bottom.Controls.Add(MakeButton("Skip  (S)", () => ShowItem(index + 1)));

        Controls.Add(picture);
        Controls.Add(guessRow);
        Controls.Add(info);
        Controls.Add(bottom);

        KeyDown += (_, e) =>
        {
            if (e.KeyCode is >= Keys.D1 and <= Keys.D3 && e.KeyCode - Keys.D1 < guesses.Count)
                Approve(guesses[e.KeyCode - Keys.D1]);
            else if (e.KeyCode == Keys.N) Reject();
            else if (e.KeyCode is Keys.S or Keys.Right) ShowItem(index + 1);
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

        string jpg = Path.ChangeExtension(json, ".jpg");
        if (File.Exists(jpg))
            picture.Image = Image.FromStream(new MemoryStream(File.ReadAllBytes(jpg))); // don't lock the file

        info.Text = $"{index + 1} of {items.Count}   |   {takenAt.Replace('T', ' ')}   |   Is it one of these?";
        for (int g = 0; g < guesses.Count; g++)
        {
            var guess = guesses[g];
            guessRow.Controls.Add(MakeButton($"{g + 1}. {guess.Common}  ({guess.Prob:P0})", () => Approve(guess)));
        }
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
                DateTime.Now.ToString("s"), takenAt, Path.GetFileName(jpg), decision, common, scientific, category,
                best?.Common ?? "", best?.Prob.ToString("0.000") ?? "",
            };
            var sb = new StringBuilder();
            if (newFile) sb.AppendLine("reviewed_at,taken_at,image,decision,common_name,scientific_name,category,best_guess,best_guess_prob");
            sb.AppendLine(string.Join(",", row.Select(v => $"\"{v.Replace("\"", "\"\"")}\"")));
            File.AppendAllText(decisionsCsv, sb.ToString());
        }

        picture.Image?.Dispose();
        picture.Image = null;
        File.Move(json, Path.Combine(destDir, Path.GetFileName(json)), overwrite: true);
        if (File.Exists(jpg)) File.Move(jpg, Path.Combine(destDir, Path.GetFileName(jpg)), overwrite: true);
        items.RemoveAt(index);
        ShowItem(index);
    }

    private static List<Species> LoadSpecies(string path)
    {
        if (!File.Exists(path)) return new();
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        return doc.RootElement.GetProperty("species").EnumerateArray()
            .Select(s => new Species(s.GetProperty("common").GetString()!, s.GetProperty("scientific").GetString()!,
                s.GetProperty("category").GetString()!))
            .OrderBy(s => s.Common, StringComparer.OrdinalIgnoreCase)
            .ToList();
    }
}
