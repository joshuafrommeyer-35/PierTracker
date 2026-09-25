"""Community viewer for the Under Scripps Pier cam (built, not live yet; see docs/COMMUNITY.md).

Serves a page that plays the camera's official player (HDOnTap's embed, as on the Scripps CoOL lab's
page) and draws boxes around what the tracker currently sees, from frames/underwater/live.json.
Anyone watching can click a box and say what they think it is. Each answer is saved with the
tracker's own picture of that moment (from frames/underwater/recent/) to community/submissions/,
and, when configured, sent to a Google Sheet (community/apps_script.gs) that has a "Reviewed by
expert" checkbox on every row. The video itself is never re-broadcast from this PC.

    tracker\\.venv\\Scripts\\python community\\server.py      (then open http://127.0.0.1:8765)

Standard library only. Listens on this PC only unless livecams.json says otherwise.
"""

import base64
import csv
import io
import json
import re
import sys
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
LIVECAMS = HERE.parent
FRAMES = LIVECAMS / "frames" / "underwater"
LIVE = FRAMES / "live.json"
RECENT = FRAMES / "recent"
SUBMISSIONS = HERE / "submissions"
SPECIES = LIVECAMS / "tracker" / "species.json"
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
FIELDS = ["submitted_at", "taken_at", "community_id", "handle", "note", "tracker_said", "tracker_prob", "status",
          "box", "picture", "reviewed_by_expert", "expert_id"]
MAX_BODY = 10_000
lock = threading.Lock()
recent_posts = defaultdict(deque)  # client address -> times of their recent submissions
served = {}  # taken_at -> the detections sent to viewers (answers must be about one of these)


def read_config():
    """The "community" section of livecams.json (which allows // comments)."""
    text, out, in_string, i = (LIVECAMS / "livecams.json").read_text(encoding="utf-8"), [], False, 0
    while i < len(text):
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))  # trailing commas
    config = json.loads(cleaned).get("community", {})
    try:  # the Sheet's address and password: in a local file, never in the (public) repo
        config.update(json.loads((HERE / "secrets.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return {"host": config.get("host", "127.0.0.1"), "port": int(config.get("port", 8765)),
            "embed": config.get("embed", "https://portal.hdontap.com/s/embed?stream=scripps_pier-underwater-CUST"
                                         "&ratio=16:9&fluid=true"),
            # The official player only loads on sites HDOnTap allows (Scripps, UCSD, DeepSea, HDOnTap). Elsewhere the
            # page offers a link to watch it there instead. Set true only when hosted on an allowed site.
            "embed_allowed": bool(config.get("embedAllowed", False)),
            "watch_url": config.get("watchUrl", "https://hdontap.com/stream/018408/scripps-pier-underwater-live-webcam/"),
            "apps_script_url": config.get("appsScriptUrl", ""), "apps_script_secret": config.get("appsScriptSecret", ""),
            "drive_folder": config.get("driveFolder", ""), "per_hour": int(config.get("maxPerHourPerViewer", 30)),
            # The tracker's own frames under the boxes. The footage belongs to Scripps/HDOnTap: served only on
            # this PC unless explicitly allowed (with their permission) in livecams.json.
            "show_frames": bool(config.get("showTrackerFrames", config.get("host", "127.0.0.1") == "127.0.0.1"))}


CONFIG = read_config()


def clean(text, limit):
    """Plain text only, no line breaks, and never something a spreadsheet would run as a formula."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).strip()[:limit]
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def species_names():
    spec = json.loads(SPECIES.read_text(encoding="utf-8"))
    names = sorted({s["common"] for s in spec["species"]} | {s["group"] for s in spec["species"] if s.get("group")})
    return names + ["not an animal"]


def save_submission(body, client):
    taken_at = str(body.get("taken_at", ""))
    if not TIME_RE.match(taken_at):
        return 400, "bad time"
    frame = RECENT / f"{taken_at[11:13]}{taken_at[14:16]}{taken_at[17:19]}.jpg"
    if not frame.exists():
        return 410, "that moment is no longer kept (only the last 10 minutes are)"
    box = body.get("box")
    if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
        return 400, "bad box"
    with lock:
        match = next((d for d in served.get(taken_at, []) if all(abs(a - b) <= 2 for a, b in zip(d["box"], box))), None)
    if match is None:
        return 400, "that box isn't one the tracker showed"
    community_id = clean(body.get("community_id"), 80)
    if not community_id:
        return 400, "please say what you think it is"
    now = time.time()
    with lock:
        posts = recent_posts[client]
        while posts and now - posts[0] > 3600:
            posts.popleft()
        if len(posts) >= CONFIG["per_hour"]:
            return 429, "thanks! that's the limit for this hour"
        posts.append(now)

    img = Image.open(frame).convert("RGB")
    x0, y0, x1, y1 = (max(0, min(int(v), lim)) for v, lim in zip(box, (img.width, img.height) * 2))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 400, "box too small"
    pad = int(0.15 * max(x1 - x0, y1 - y0))
    crop = img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))
    crop.thumbnail((640, 640))
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=88)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    picture = f"{stamp}.jpg"
    row = {"submitted_at": datetime.now().isoformat(timespec="seconds"), "taken_at": taken_at,
           "community_id": community_id, "handle": clean(body.get("handle"), 40), "note": clean(body.get("note"), 200),
           "tracker_said": match["name"], "tracker_prob": f"{match['prob']:.2f}", "status": match["status"],
           "box": " ".join(str(int(v)) for v in match["box"]), "picture": picture,
           "reviewed_by_expert": "", "expert_id": ""}

    with lock:
        for folder in [SUBMISSIONS] + ([Path(CONFIG["drive_folder"])] if CONFIG["drive_folder"] else []):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / picture).write_bytes(buf.getvalue())
                sheet = folder / "submissions.csv"
                new = not sheet.exists()
                with sheet.open("a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, FIELDS)
                    if new:
                        w.writeheader()
                    w.writerow(row)
            except OSError as e:
                print("could not save to", folder, e, file=sys.stderr)
    if CONFIG["apps_script_url"]:
        threading.Thread(target=send_to_sheet, args=(row, buf.getvalue()), daemon=True).start()
    return 200, "thanks! saved for review"


def send_to_sheet(row, jpeg):
    """To the Google Sheet (apps_script.gs), in the background so a slow network never blocks anyone."""
    payload = dict(row, secret=CONFIG["apps_script_secret"], image_jpeg_base64=base64.b64encode(jpeg).decode())
    req = urllib.request.Request(CONFIG["apps_script_url"], data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except OSError as e:
        print("could not reach the Google Sheet:", e, file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    server_version = "PierTrackerCommunity/1"

    def _send(self, code, body, kind="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            if CONFIG["embed_allowed"]:
                live = (f'<iframe id="live" src="{CONFIG["embed"]}" allow="autoplay; fullscreen" allowfullscreen '
                        'title="Scripps Pier underwater camera (HDOnTap)" hidden></iframe>')
            else:
                live = ('<div id="live" class="golive" hidden><p>The live player can only be shown on the camera '
                        "owners' own sites.</p>"
                        f'<a class="primary" href="{CONFIG["watch_url"]}" target="pierlive" rel="noopener">'
                        'Watch live on HDOnTap &#8599;</a><p class="small">It opens in its own window: put it next '
                        "to this one. The boxes here follow the tracker's snapshots.</p></div>")
            page = (HERE / "index.html").read_text(encoding="utf-8").replace("{{LIVE}}", live)
            self._send(200, page, "text/html; charset=utf-8")
        elif path == "/detections":
            try:
                live = json.loads(LIVE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                live = {"status": "no data", "detections": []}
            live["server_time"] = datetime.now().isoformat(timespec="seconds")
            if CONFIG["show_frames"] and live.get("taken_at") and TIME_RE.match(live["taken_at"]):
                t = live["taken_at"]
                if (RECENT / f"{t[11:13]}{t[14:16]}{t[17:19]}.jpg").exists():
                    live["frame"] = f"/frame/{t[11:13]}{t[14:16]}{t[17:19]}.jpg"
            if live.get("taken_at") and TIME_RE.match(live["taken_at"]):
                with lock:
                    served[live["taken_at"]] = live.get("detections", [])
                    for old in sorted(served)[:-120]:  # ~10 minutes of frames
                        del served[old]
            self._send(200, json.dumps(live))
        elif path.startswith("/frame/") and CONFIG["show_frames"] and re.fullmatch(r"/frame/\d{6}\.jpg", path):
            try:
                self._send(200, (RECENT / path[7:]).read_bytes(), "image/jpeg")
            except OSError:
                self._send(404, json.dumps({"error": "gone"}))
        elif path == "/species":
            self._send(200, json.dumps(species_names()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/submit":
            return self._send(404, json.dumps({"error": "not found"}))
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self._send(413, json.dumps({"message": "too big"}))
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send(400, json.dumps({"message": "bad request"}))
        code, message = save_submission(body if isinstance(body, dict) else {}, self.client_address[0])
        self._send(code, json.dumps({"message": message}))

    def log_message(self, fmt, *args):  # quiet: no per-request lines
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((CONFIG["host"], CONFIG["port"]), Handler)
    print(f"community viewer on http://{CONFIG['host']}:{CONFIG['port']}")
    server.serve_forever()
