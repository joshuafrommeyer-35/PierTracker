# Community IDs (built, not public yet)

Anyone watching can click a box the tracker drew around an animal and say what they think it is.
Each answer is saved with the tracker's own picture of that moment and goes to a Google Sheet where
every row has a **Reviewed by expert** checkbox.

Status: built and tested on this PC (2026-09-25). **Not public**: it needs the camera owners' OK first
(see [Permission](#permission)).

## Pieces

| File | What it does |
|---|---|
| `community/server.py` | Small web server (Python standard library). Serves the page, the tracker's current boxes, and receives answers. Listens on this PC only (`127.0.0.1:8765`) unless `livecams.json` says otherwise. |
| `community/index.html` | The page: the picture with clickable boxes (solid orange = logged, dashed = the tracker isn't sure), a form, and a sync slider for the live-video mode. |
| `community/apps_script.gs` | The Google Sheet side: adds a row per answer, saves the picture to a Drive folder, and puts a checkbox in "Reviewed by expert". |
| `frames/underwater/live.json` | Written by the tracker every frame: what it sees, with boxes, names and whether it logged them. |
| `community/secrets.json` | **Local only, never committed.** The Sheet's web-app address and the password the server sends with each answer. |

Try it on this PC:

```powershell
tracker\.venv\Scripts\python community\server.py
# open http://127.0.0.1:8765
```

Answers land in `community/submissions/` (picture + `submissions.csv`), and in the Google Sheet once it's set up.

### Two views

- **Tracker view** (default): the tracker's own latest frame with the boxes exactly in place. It's a
  slideshow, one snapshot about every 10 s (every ~3 s while it follows a fish), and says so on screen.
  Hovering over a box holds the picture still so it can be clicked. Only shown on this PC, because the
  footage belongs to Scripps/HDOnTap (`showTrackerFrames` in `livecams.json` turns it on elsewhere, with
  their OK).
- **Live video**: the official player with the boxes on top, after an adjustable delay. The player
  **only loads on sites HDOnTap allows** (below), so it's blank here.

### What visitors can and can't do

- Answer only about a box the tracker actually showed: the server remembers the boxes it served (10
  minutes) and rejects anything else; the picture and "tracker said" come from its own records.
- Text is limited (ID 80 characters, name 40, note 200), cleaned of control characters, and can never
  run as a spreadsheet formula.
- At most 30 answers per visitor per hour (`maxPerHourPerViewer`).
- Nothing on this PC is reachable except these four addresses: the page, `/detections`, `/species`,
  `/submit` (and `/frame/...` when frames are shared).

## Google Sheet setup (one time, ~2 minutes)

Use your review Sheet (its link is kept in `community/secrets.json`, not in this public repo).

1. Open the Sheet > **Extensions > Apps Script**.
2. Delete what's there and paste the contents of **`community/apps_script.local.gs`** (local only: it
   already has this PC's password filled in). Save.
3. **Deploy > New deployment > Web app**. Execute as: **Me**. Who has access: **Anyone**. Deploy, and
   approve the access it asks for (it only uses this Sheet and one Drive folder).
4. Copy the web app URL into `community/secrets.json` as `"appsScriptUrl"` (or send it to Claude).

Rows then look like: Submitted, Seen at, Their ID, By, Note, Tracker said, Tracker confidence, Tracker
status, Picture (Drive link), **Reviewed by expert** (checkbox), Expert's ID.

## Permission

HDOnTap only lets the underwater player be embedded on these sites (its `frame-ancestors` policy):
Scripps, UCSD (pierviz, coollab, aquarium), DeepSea and HDOnTap itself. The camera is public to
*watch*, but that list is the owner saying where it may be *embedded*. Getting around it (proxying or
re-streaming the video, faking where a request comes from) would mean bypassing their restriction and
re-broadcasting their footage, so this project doesn't. The ways forward:

1. **Ask** the Scripps Coastal Observing lab (CoOL, who run pierviz) and/or HDOnTap. A community-science
   overlay fits their outreach, and there are two easy yeses they could give: add this site to the
   embed list, or host the page on their own site (already on the list). Draft below.
2. **Without permission**, still possible: publish only the tracker's own data (names, times, counts;
   already public in the README), and link to the official live page. The ID tool could then work as a
   "second screen": the official stream open in one window, and in another a simple outline of the
   scene (drawn by us, not footage) with the live boxes on it. The pictures stay private to the review
   Sheet. Clunkier, but needs nobody's OK.

### Draft email

> **Subject:** A community-science idea for the Under Scripps Pier camera
>
> Hi CoOL team,
>
> I've been running an open-source project, PierTracker (github.com/joshuafrommeyer-35/PierTracker),
> that watches the Under Scripps Pier camera and logs the animals it sees (species, times, counts, with
> the pier's temperature, tide and turbidity), with every uncertain sighting reviewed by a person. It
> publishes daily summaries; no footage is republished.
>
> I'd like to add a community-science page where viewers click a box the tracker drew and say what they
> think it is, with an expert reviewing every answer. It needs the live player on the page, and the
> HDOnTap embed is limited to your sites. Would you be open to either adding [site] to the embed list,
> or hosting the page on one of your sites? Happy to share the code, the data, or anything useful to you.
>
> Thanks for running the camera. It's been fun and a great way to learn.

## Hosting (when it's time)

The page and answers are tiny; the video always comes from HDOnTap, never from here. Options, safest first:

| Option | Cost | How it works | Notes |
|---|---|---|---|
| **Host on Scripps/UCSD's site** | free | They host `index.html`; answers go to your Google Sheet via the Apps Script. Your PC only uploads `live.json`. | Also solves the embed permission. Best outcome of the email. |
| **Cloudflare Pages + a Worker** | free tier | Page on Cloudflare Pages; your PC pushes `live.json` every few seconds to a Worker (KV) and answers go browser -> Worker -> Apps Script. Nothing ever connects *to* your PC. | Most secure and lightest for the PC; a bit more setup. Custom domain optional (~$10/yr). |
| **Cloudflare Tunnel** | free (domain ~$10/yr) | `cloudflared` on the PC opens an outbound tunnel to Cloudflare; visitors reach `server.py` through it. No open ports on your router, HTTPS included, Cloudflare's DDoS protection in front. | Simplest step up from the local test. Add Cloudflare Access for a private beta (sign-in by email). |
| **Tailscale Funnel** | free (personal) | One command shares the local port at an `https://<name>.ts.net` address. | Great for a quick test with friends; not meant for heavy public traffic. |
| **Zooniverse** | free | Purpose-built for citizen science: upload the tracker's pictures as "subjects", volunteers classify, built-in consensus and expert review, big volunteer community. | Not live video, and needs the owners' OK for the pictures. Good fit if live isn't essential. |

Never: port-forwarding on the home router, or running the server as an administrator.

## Next steps

1. Email CoOL/HDOnTap (draft above).
2. Set up the Google Sheet (above) and test with a few answers from this PC.
3. With a yes: pick hosting (Scripps's site, or Cloudflare Pages + Worker), set `host`/`showTrackerFrames`
   accordingly, and have LiveCams start `server.py` with the tracker.
4. Feed expert-checked answers into the review data (as reviewer `community`), so they teach the
   tracker too.
