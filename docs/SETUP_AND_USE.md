# Setting up and using LiveCams

Back to the [README](../README.md). How it works: [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

## Setup

**The easy way:** double-click **`Set up and start LiveCams.bat`** in this folder. It checks every step and
skips what's already done, so it's safe to run any time, including after a Windows reset or on a new PC:

1. Installs what's missing with `winget`: Python 3.12, the .NET 8 SDK, Git, the WebView2 runtime.
2. **Restores your data** from the Google Drive backup if this PC has none. It only fills in missing
   files and never overwrites anything. If the backup is configured but Drive isn't signed in yet, it
   stops rather than starting with empty data (which the nightly backup could then copy over the real
   one). Run it with `-Fresh` to start empty on purpose.
3. Builds the tracker's Python environment and models if they're missing or broken. The only thing it
   ever deletes is a `tracker\.venv` that no longer runs, and it rebuilds that.
4. Builds the app (`-Rebuild` forces it), starts the cams, the tracker and the tray icon (through the
   Wallpaper Manager if it's in the parent folder), pins the tray icon to the taskbar, and checks you're
   signed in to GitHub for the nightly results.

**By hand**, the same steps. Requirements:
- Windows 10 or 11
- [.NET 8 SDK](https://dotnet.microsoft.com/download) to build the app
- WebView2 Runtime (preinstalled on Windows 11)
- Python 3.12 for the tracker
- Optional: an Intel integrated GPU for the tracker's models

```powershell
git clone https://github.com/joshuafrommeyer-35/PierTracker.git
cd PierTracker

# 1. Build the wallpaper app into .\app
dotnet publish host -c Release -o app

# 2. Tracker environment (CPU-only PyTorch first so nothing pulls a CUDA build)
py -3.12 -m venv tracker\.venv
tracker\.venv\Scripts\pip install torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cpu
tracker\.venv\Scripts\pip install -r tracker\requirements-setup.txt -r tracker\requirements-ml.txt

# 3. Download and convert the models (about 1.9 GB download, 2-3 minutes)
tracker\.venv\Scripts\python tracker\setup_models.py

# 4. Check livecams.json: monitors are numbered left to right. Then start it:
app\LiveCams.exe --on
```

Re-run `setup_models.py` after editing `tracker/species.json`. To publish results to your own fork, set
`"publishResults": true` in `livecams.json`. It commits and pushes with your normal git credentials.

## Usage

| Do this | What happens |
|---|---|
| `app\LiveCams.exe --on` | Cams and tracker start, and start again at every login |
| `app\LiveCams.exe --off` | Each monitor freezes on its current cam picture (saved as a normal Windows wallpaper), then everything shuts down. Nothing keeps running. Removes the login start |
| `app\LiveCams.exe --quit` | Shuts down without freezing; your normal wallpaper comes back |
| Click the empty desktop on a cam's monitor | Resumes that cam, or reloads it if it's broken. On the underwater monitor: go live again |
| Tray icon (wave) | Status on hover. Menu: review uncertain sightings, **rewind the underwater cam**, **just saw something? keep the last 5 minutes**, **pause cams + tracker** (for demanding games), resume a cam, reload, open the folder, turn off. Double-click resumes everything. An amber dot means sightings are waiting for review; grey means paused |
| `app\LiveCams.exe --relaunch` | Restart it (e.g. after an update), keeping the start-at-login setting and any pause |
| `app\LiveCams.exe --pause` / `--resume` | Pause: each monitor keeps a still, the cams unload and the tracker stops (its memory is freed). It stays paused, across restarts too, until resumed (or `--on`). Full-screen games already pause the cams automatically; this makes sure, e.g. for a game in a borderless window |
| `app\LiveCams.exe --review` | The review window on its own, even while the cams are off |
| `app\LiveCams.exe --rewind` | The rewind window on its own (see [Rewind](HOW_IT_WORKS.md#rewind-hostrewindcs)). Keys: Space play/pause, ←/→ 10 s, Shift+←/→ 1 min, `,` `.` one frame, `[` `]` speed |

### Reviewing uncertain sightings

**Reminder.** While sightings are waiting, the tray icon has an amber dot and its tooltip gives the
count. At most once a day, when 10 or more are waiting, a Windows notification says so; clicking it
opens the review window. It only appears while you're using the PC, never during a game or other
full-screen app or in the first 15 minutes after login, and Windows' Do Not Disturb silences it.

Tray icon → **Review uncertain sightings (N)...** shows each saved picture: the close-up on the left, and
where it was in the frame on the right. The bottom line shows how many answers each animal has toward the
camera-trained classifier. The top line says which kind it is:
- **"The tracker wasn't sure. Is it one of these?"**
- **"The tracker logged this as a ___. Is that right?"** Press `1` if it's right. Otherwise pick the
  right animal, or press `N`.

| Key | Action |
|---|---|
| `1` `2` `3` | It's the tracker's 1st, 2nd or 3rd guess |
| Pick from the list → **Approve as this** | It's a different animal. The list ends with the look-alike groups ("group: silversides & sardines") for when you can tell the kind of fish but not the species |
| `N` | Not an animal |
| `S` or `→` | Skip for now |
| `C` | Copy the picture, to paste into a chat or iNaturalist when you want help with the ID (skip it meanwhile; it stays in the queue) |

Some cards show a **Suggestion** line (for example from Claude going through the queue): a second opinion
with the reason, never applied by itself.

**Compare panel (right).** Each guess, the suggestion, and whatever you pick from the list is shown with
three typical reference photos and a line on how to tell it apart ("black spot at the base of the tail").
The photos are the research-grade iNaturalist reference photos the tracker already keeps locally, chosen
as the most typical underwater ones of each species (`reference_photos.py examples`). Blacksmith leads with
a young one, since those are what this camera sees most. Click a photo to open it full size. The notes are
in [`tracker/field_marks.json`](../tracker/field_marks.json). If you still can't tell, skip: a wrong answer
teaches the tracker the wrong thing, and a skipped one costs nothing.

Each answer records who gave it (`reviewer`: `person`, or `claude` for structure, empty water and clear
lobster pictures checked by eye). Only a person's answers count toward the published **Checked** and
**Confirmed by hand**; all answers teach the tracker. An answer to an "Is this right?" picture also
**corrects the sighting it came from**: the database keeps the logged name and adds the corrected one
(`corrected_name`, empty for "not an animal"), and the statistics use the correction.

Decisions go to `data/review/decisions.csv`, and the pictures move to `data/review/approved` or
`data/review/rejected`. The next daily publish lists confirmed animals under **Confirmed by hand**.

### `livecams.json`

| Setting | Meaning |
|---|---|
| `cams[].monitor` | Monitor number, counted left to right |
| `cams[].page` / `iframe` | The official page, and the CSS selector of its player |
| `cams[].extraBottomPx` | Pushes a toolbar under the video off-screen (Surfline: 40) |
| `cams[].mode` | `alwaysOn` (keep streaming) or `resumeWhenWatched` (follow the player's own pause) |
| `cams[].freezeDisplayAfterSeconds` | Freeze the on-screen picture after this long, while the stream keeps running |
| `cams[].captureEverySeconds` / `captureDir` | Save a frame for the tracker this often (2 s; its snapshots stay one every ~10 s) |
| `cams[].rewind` | Keep the cam's video for rewinding: `dir` (folder, default `video`), `maxGB` (default 50, ~24 h), `minFreeGB` (always leave this much free, default 50), `skipDark` (skip night, default true). Leave out to keep nothing |
| `watchedIdleSeconds` / `coverThreshold` | What counts as "someone is looking" |
| `pauseDuringFullscreenApps` | Unload the cams during games and other full-screen apps |
| `notGames` | Full-screen programs that don't pause the cams (default: browsers, so watching the cam itself full-screen doesn't blind the tracker) |
| `reviewReminderHours` / `reviewReminderMinPending` | Review notification at most this often (default 24 h; 0 = tray dot only), and only with at least this many waiting (default 10) |
| `tracker.enabled` / `publishResults` | Run the tracker; push daily results to GitHub |
| `tracker.backupDir` | Folder for the nightly backup (e.g. on Google Drive); leave out for none |
| `debugPort` | Troubleshooting only (Chrome DevTools on localhost). Keep at 0 |

### Files it writes (local only, not committed)

| Path | Contents |
|---|---|
| `data/piertracker.db` | **Everything the tracker saw** (SQLite): snapshots, sightings, visits, species, conditions, reviews |
| `data/crops/<date>/` | Sample crops for validation (kept 14 days) |
| Google Drive `PierTracker backup/` | Nightly copy of the database, review answers, CSVs and the whole frame bank (`tracker.backupDir` in `livecams.json`). Each file is copied under a temporary name and swapped in, so an interrupted backup leaves the previous one whole, and a PC with less data than the backup (a fresh install that wasn't restored) never overwrites it |
| `data/background.png` | The long-term background (median of the last hour of clear daylight) that fixed, swaying things are compared with |
| `data/fixture_gallery.npz` | Person-confirmed fixtures and animals (from review answers), with where they were |
| `data/frame_bank/<date>/` | Sample full frames kept for training a detector on this camera later (90 days, ~20 MB/day) |
| `data/review/` | Review pictures: `pending/`, `approved/`, `rejected/` and `decisions.csv` |
| `sandbox/piertracker_sandbox.db` | Your practice copy (`sql.py`, `.reset` to refresh) |
| `data/environment_hourly.csv` | Hourly conditions at the pier (NOAA + SCCOOS), temperature anomaly, El Niño index |
| `data/ml/classifier_report.md` | Latest camera-classifier training report |
| `logs/nightly.log` | What the nightly job did |
| `frames/` | The latest tracker frame, and the stills used by `--off` |
| `frames/underwater/recent/` | The last 10 minutes of frames (daylight, every 2 s), with what was ignored as fixed structure in each (to check "did it see that?") |
| `video/rewind/` | The underwater cam's video, as the stream's own 10-second pieces, named by when they were on screen. Daylight only, up to `rewind.maxGB` (50 GB, about the last two days), oldest deleted first |
| `video/saved/` | Clips and pictures kept from the rewind window or the tray. Never deleted automatically |
| `data/archive/` | Earlier data set aside, never deleted: pre-tuning runs, the CSV logs used before the database, removed fixture sightings |
| `logs/host.log`, `logs/tracker.log` | What the app and tracker did |

## Project layout

```
host/                 LiveCams wallpaper app (C#, .NET 8, WebView2)
  Rewind.cs           keeps the underwater video; the rewind window
  viewer/             the rewind window's page, with hls.js (Apache-2.0) to play the video
tracker/
  tracker.py          the background tracker
  setup_models.py     one-time model download + OpenVINO conversion
  environment.py      hourly conditions at the pier (NOAA tide gauge + SCCOOS shore station),
                      temperature anomaly and El Nino index
  nightly.py          once-a-day job: conditions, database sync, retraining, conditions model, publish
  db.py               the SQLite database (schema, recording, sync, sandbox copy)
  sql.py              small SQL shell for practicing on the sandbox
  ml/
    train_classifier.py  camera-trained classifier from review answers
    conditions_model.py  "what brings animals in" regressions
    reference_photos.py  reference-photo classifier (shadow mode) and the review window's example photos
    species_eval.py      tests a candidate species before it's added
  publish_results.py  daily README/results update
  species.json        the animals it can name (edit to taste)
results/              published summaries: daily, hourly sightings, hourly conditions,
                      validation, confirmed by hand
livecams.json         configuration
setup.ps1             set up and start (run by "Set up and start LiveCams.bat")
tests/                automated tests (python -m pytest tests); run on GitHub for every code change
community/            community IDs: viewers click a box and say what it is (built, not public yet:
                      docs/COMMUNITY.md)
docs/                 how it works, validation, setup and use, the SQL walkthrough
```
