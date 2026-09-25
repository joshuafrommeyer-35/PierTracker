"""Animal tracker for the Under Scripps Pier cam.

Whenever the LiveCams wallpaper saves a new frame (frames/underwater/latest.jpg,
about every 10 s while the stream plays), this finds fish with the Community Fish
Detector, names each one with BioCLIP 2, looks for animals the fish detector won't
box (octopus, crabs, jellies, sea lions, divers...) in the whole frame and in six
overlapping tiles, and appends what it saw to data/sightings.csv. Sightings it isn't
sure about are saved to data/review/pending for a person to approve or correct.

Built to be invisible: the models run through OpenVINO on the Intel iGPU when there
is one (so the CPU cores and the Radeon stay free), otherwise on two efficiency
cores; LiveCams launches this in Windows Efficiency mode; and dark (night) frames
are skipped before any model runs. When the wallpaper is paused, frozen or off, no
new frames arrive and this just sleeps.

Needs the files made by setup_models.py.
"""

import csv
import ctypes
import gc
import io
import json
import logging
import logging.handlers
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import openvino as ov
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
LIVECAMS = ROOT.parent
FRAME = LIVECAMS / "frames" / "underwater" / "latest.jpg"
MODELS = ROOT / "models"
DATA = LIVECAMS / "data"
SIGHTINGS_CSV = DATA / "sightings.csv"
HOURLY_CSV = DATA / "hourly_summary.csv"
HOUR_STATE = DATA / ".current_hour.json"
CROPS = DATA / "crops"
REVIEW = DATA / "review" / "pending"

POLL_SECONDS = 2
STALE_SECONDS = 60          # a frame older than this means the wallpaper isn't streaming
DARK_MEAN = 20              # mean brightness (0-255) below which a frame is "night"
DARK_DETAIL = 4.0           # detail left after blurring; night sensor noise averages away to ~0
DETECT_THRESHOLD = 0.35     # fish detector confidence
SPECIES_MIN_PROB = 0.60     # below this a detected fish is logged as unidentified
NEGATIVE_MIN_PROB = 0.50    # a detection this sure to be murk/kelp/piling is dropped
SCENE_MIN_PROB = 0.70       # whole-frame check for big animals (must beat every label)
BIG_BOX_FRACTION = 0.25     # a detection this big is the animal the whole-frame check found
TILE_EVERY = 3              # scan the six tiles every 3rd frame (30 s): slow movers don't need more
REVIEW_FISH_PROB = 0.25     # an unidentified fish whose best guess is at least this goes to review
REVIEW_SCENE_PROB = 0.40    # a non-fish/big-animal guess between this and SCENE_MIN_PROB goes to review
REVIEW_EVERY = timedelta(minutes=10)  # at most one review picture per best guess per 10 min
REVIEW_MAX_PENDING = 300    # stop adding review pictures while this many wait
MAX_CROPS = 12
MIN_CROP_PX = 20
KEEP_CROPS_DAYS = 14
UNLOAD_AFTER = timedelta(minutes=10)  # free the models' ~1 GB at night and while the cams are paused
CROP_EVERY = timedelta(minutes=10)  # keep one sample crop per animal type per 10 min, for review
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)

SIGHTING_FIELDS = ["timestamp", "date", "time", "common_name", "scientific_name", "category", "count", "confidence"]
HOURLY_FIELDS = ["date", "hour", "snapshots_analyzed", "snapshots_dark", "common_name", "snapshots_seen", "max_count"]
UNIDENTIFIED = {"common": "fish (unidentified)", "scientific": "", "category": "fish"}

log = logging.getLogger("tracker")


@dataclass
class Sighting:
    common: str
    scientific: str
    category: str
    count: int
    confidence: float


class Models:
    def __init__(self):
        core = ov.Core()
        core.set_property({"CACHE_DIR": str(MODELS / "cache")})  # skip GPU kernel compiles after the first run
        self.device = integrated_gpu(core)
        if self.device:
            # Low queue/host priority: the CPU sleeps while the iGPU works instead of spinning
            # (measured: ~156 ms -> ~3-25 ms of CPU per inference, same speed).
            config = {"PERFORMANCE_HINT": "LATENCY", "GPU_QUEUE_THROTTLE": "LOW", "GPU_HOST_TASK_PRIORITY": "LOW"}
        else:
            self.device = "CPU"
            config = {"PERFORMANCE_HINT": "LATENCY", "INFERENCE_NUM_THREADS": 2,
                      "SCHEDULING_CORE_TYPE": "ECORE_ONLY", "ENABLE_HYPER_THREADING": False}
        self.detector = core.compile_model(MODELS / "detector.xml", self.device, config)
        self.classifier = core.compile_model(MODELS / "classifier.xml", self.device, config)

        det_shape = self.detector.input(0).get_shape()
        self.det_size = int(det_shape[2])
        self.det_boxes = next(o for o in self.detector.outputs if o.get_partial_shape()[-1].get_length() == 4)
        self.det_logits = next(o for o in self.detector.outputs if o != self.det_boxes)

        meta = json.loads((MODELS / "classifier.json").read_text(encoding="utf-8"))
        self.cls_size = meta["size"]
        self.cls_mean = np.array(meta["mean"], np.float32)
        self.cls_std = np.array(meta["std"], np.float32)
        self.logit_scale = meta["logit_scale"]
        self.labels = meta["labels"]
        self.label_emb = np.load(MODELS / "label_embeddings.npy")
        log.info("models ready on %s (%d labels)", self.device, len(self.labels))

    def detect(self, img: Image.Image):
        """Fish boxes as (x0, y0, x1, y1, score) in image pixels."""
        x = np.asarray(img.resize((self.det_size, self.det_size), Image.BILINEAR, reducing_gap=2.0), np.float32) / 255.0
        x = ((x - IMAGENET_MEAN) / IMAGENET_STD).transpose(2, 0, 1)[None]
        out = self.detector(x)
        boxes, logits = out[self.det_boxes][0], out[self.det_logits][0]
        scores = 1.0 / (1.0 + np.exp(-logits.max(axis=1)))  # every class the detector has means "fish"
        keep = np.argsort(-scores)[:MAX_CROPS * 4]
        keep = keep[scores[keep] >= DETECT_THRESHOLD]
        w, h = img.size
        result = []
        for i in keep:
            cx, cy, bw, bh = boxes[i]
            result.append(((cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h, float(scores[i])))
        return nms(result)[:MAX_CROPS]

    def embed(self, img: Image.Image) -> np.ndarray:
        size = (self.cls_size, self.cls_size)
        x = np.asarray(img.convert("RGB").resize(size, Image.BICUBIC, reducing_gap=2.0), np.float32) / 255.0
        x = ((x - self.cls_mean) / self.cls_std).transpose(2, 0, 1)[None]
        return self.classifier(x)[0][0]

    def probabilities(self, embedding: np.ndarray) -> np.ndarray:
        logits = self.logit_scale * self.label_emb @ embedding
        p = np.exp(logits - logits.max())
        return p / p.sum()


def file_safe(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name)


def integrated_gpu(core: ov.Core):
    """The integrated (Intel) GPU, if any. Never a discrete card: that one is for games."""
    for device in core.available_devices:
        if device.startswith("GPU") and core.get_property(device, "DEVICE_TYPE") == ov.properties.device.Type.INTEGRATED:
            return device
    return None


def nms(boxes, iou_limit=0.5):
    kept = []
    for b in sorted(boxes, key=lambda b: -b[4]):
        if all(iou(b, k) < iou_limit for k in kept):
            kept.append(b)
    return kept


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def square_crop(img: Image.Image, box, pad=0.15):
    """The box, padded and made square so the classifier doesn't see a squashed fish. Near an
    edge the square slides inward rather than filling with black."""
    x0, y0, x1, y1 = box[:4]
    side = min(max(x1 - x0, y1 - y0) * (1 + 2 * pad), img.width, img.height)
    left = min(max((x0 + x1 - side) / 2, 0), img.width - side)
    top = min(max((y0 + y1 - side) / 2, 0), img.height - side)
    return img.crop((int(left), int(top), int(left + side), int(top + side)))


def tile_boxes(img: Image.Image):
    """Six overlapping squares, 3 across and 2 down, each 70% of the frame height. A small
    octopus or crab that is lost in the whole frame fills a good part of one tile."""
    w, h = img.size
    side = int(h * 0.7)
    xs = np.linspace(0, w - side, 3).astype(int)
    ys = np.linspace(0, h - side, 2).astype(int)
    return [(int(x), int(y), int(x) + side, int(y) + side) for y in ys for x in xs]


def top_guesses(labels, probs, n=3):
    """The n most likely animals (never the "not an animal" labels), for the review queue."""
    order = [i for i in np.argsort(-probs) if not labels[i]["negative"]][:n]
    return [{"common": labels[i]["common"], "scientific": labels[i]["scientific"],
             "category": labels[i]["category"], "prob": round(float(probs[i]), 3)} for i in order]


def review_card(view: Image.Image, frame: Image.Image, box, guesses, when: datetime) -> Image.Image:
    """One picture per uncertain sighting: the close-up, where it was in the frame, and the
    tracker's top guesses. Readable on its own in File Explorer, and shown by the review window."""
    thumb = frame.convert("RGB")
    scale = 540 / thumb.width
    thumb = thumb.resize((540, int(thumb.height * scale)))
    card = Image.new("RGB", (1000, max(440, thumb.height + 60 + 28 * (len(guesses) + 1))), (24, 28, 34))
    close = view.convert("RGB")
    close.thumbnail((400, 400))
    card.paste(close, (20 + (400 - close.width) // 2, 20 + (400 - close.height) // 2))
    draw = ImageDraw.Draw(thumb)
    draw.rectangle([v * scale for v in box], outline=(255, 64, 64), width=3)
    card.paste(thumb, (440, 20))
    draw = ImageDraw.Draw(card)
    font = ImageFont.load_default(size=18)
    y = 40 + thumb.height
    draw.text((440, y), when.strftime("%Y-%m-%d %H:%M:%S"), fill=(200, 200, 200), font=font)
    for i, g in enumerate(guesses, 1):
        draw.text((440, y + 28 * i), f"{i}. {g['common']}  {g['prob']:.0%}", fill=(240, 240, 240), font=font)
    return card


def is_dark(img: Image.Image) -> bool:
    gray = np.asarray(img.convert("L").resize((64, 36), Image.BOX), np.float32)
    return gray.mean() < DARK_MEAN or gray.std() < DARK_DETAIL


class Tracker:
    def __init__(self, publish=False):
        self.publish = publish
        self._models = None
        self.last_model_use = datetime.now()
        DATA.mkdir(parents=True, exist_ok=True)
        self.hour = self._load_hour()
        self.last_prune = datetime.min
        self.last_crop = {}
        self.last_review = {}
        self.frames_seen = 0

    def process(self, img: Image.Image, when: datetime):
        self._roll_hour(when)
        self.hour["analyzed"] += 1
        if is_dark(img):
            self.hour["dark"] += 1
            self._save_hour()
            return []

        started = time.perf_counter()
        sightings = self._identify(img, when)
        for s in sightings:
            seen = self.hour["species"].setdefault(s.common, [0, 0])
            seen[0] += 1
            seen[1] = max(seen[1], s.count)
        self._save_hour()
        if sightings:
            self._append(SIGHTINGS_CSV, SIGHTING_FIELDS, [
                [when.isoformat(timespec="seconds"), when.date().isoformat(), when.strftime("%H:%M:%S"),
                 s.common, s.scientific, s.category, s.count, f"{s.confidence:.2f}"] for s in sightings])
        log.info("%s: %s (%.2fs)", when.strftime("%H:%M:%S"),
                 ", ".join(f"{s.count} {s.common}" for s in sightings) or "nothing",
                 time.perf_counter() - started)
        return sightings

    def _identify(self, img: Image.Image, when: datetime):
        m = self.models()
        found = defaultdict(lambda: [0, 0.0, None])  # common name -> [count, best probability, label]

        def add(label, prob, count=1):
            entry = found[label["common"]]
            entry[0] += count
            entry[1] = max(entry[1], prob)
            entry[2] = label

        # 1. Animals the fish detector won't box (octopus, crabs, jellies, sea lions, divers, big
        #    rays...): ask the classifier about the whole frame, and every 3rd frame about six
        #    tiles too. A "scene" label only counts when it beats every other label, fish and
        #    "murky water" included; a close call goes to the review queue instead.
        full = (0, 0, img.width, img.height)
        self.frames_seen += 1
        views = [full] + (tile_boxes(img) if self.frames_seen % TILE_EVERY == 0 else [])
        whole_frame_animal = None
        for box in views:
            view = img if box == full else img.crop(box)
            probs = m.probabilities(m.embed(view))
            best = int(probs.argmax())
            label = m.labels[best]
            if not label["scene"] or label["negative"]:
                continue
            if probs[best] >= SCENE_MIN_PROB:
                if label["common"] not in found:
                    self._save_crop(view, when, label["common"], float(probs[best]))
                    add(label, float(probs[best]))
                if box == full:
                    whole_frame_animal = label
            elif probs[best] >= REVIEW_SCENE_PROB:
                self._queue_review(view, img, box, top_guesses(m.labels, probs), when)

        # 2. Fish: box each one, then name it.
        frame_area = img.width * img.height
        for box in m.detect(img):
            w, h = box[2] - box[0], box[3] - box[1]
            if min(w, h) < MIN_CROP_PX:
                continue
            if whole_frame_animal and w * h > BIG_BOX_FRACTION * frame_area:
                continue  # the big animal the whole-frame check already named
            crop = square_crop(img, box)
            probs = m.probabilities(m.embed(crop))
            best = int(probs.argmax())
            label = m.labels[best]
            if label["negative"] and probs[best] >= NEGATIVE_MIN_PROB:
                continue  # murk, kelp or piling, not an animal
            if label["negative"] or probs[best] < SPECIES_MIN_PROB:
                guesses = top_guesses(m.labels, probs)
                if guesses and guesses[0]["prob"] >= REVIEW_FISH_PROB:
                    self._queue_review(crop, img, box[:4], guesses, when)
                label, prob = UNIDENTIFIED, float(box[4])  # a fish, but not sure which: don't guess
            else:
                prob = float(probs[best])
            add(label, prob)
            self._save_crop(crop, when, label["common"], prob)

        return [Sighting(l["common"], l["scientific"], l["category"], n, p) for n, p, l in found.values()]

    def _queue_review(self, view: Image.Image, frame: Image.Image, box, guesses, when: datetime):
        """Saves an uncertain sighting for a person to approve or correct (the "Review
        uncertain sightings" window in the LiveCams tray menu)."""
        name = guesses[0]["common"]
        if when - self.last_review.get(name, datetime.min) < REVIEW_EVERY:
            return
        REVIEW.mkdir(parents=True, exist_ok=True)
        if sum(1 for _ in REVIEW.glob("*.json")) >= REVIEW_MAX_PENDING:
            return
        self.last_review[name] = when
        stem = REVIEW / f"{when:%Y%m%d-%H%M%S}_{file_safe(name)}"
        review_card(view, frame, box, guesses, when).save(stem.with_suffix(".jpg"), quality=88)
        stem.with_suffix(".json").write_text(json.dumps({
            "taken_at": when.isoformat(timespec="seconds"),
            "box": [round(float(v)) for v in box],
            "guesses": guesses,
        }, indent=1), encoding="utf-8")
        log.info("queued for review: %s? (%.0f%%)", name, 100 * guesses[0]["prob"])

    def models(self) -> Models:
        """Loads the models on first use (about 2 s with the compiled-model cache)."""
        self.last_model_use = datetime.now()
        if self._models is None:
            self._models = Models()
        return self._models

    def unload_if_idle(self):
        if self._models is not None and datetime.now() - self.last_model_use > UNLOAD_AFTER:
            self._models = None
            gc.collect()
            log.info("models unloaded (idle)")

    def _save_crop(self, crop: Image.Image, when: datetime, name: str, prob: float):
        """Saves a sample of what was identified, named <time>_<animal>_<confidence>.jpg, so
        people can check the tracker's work (see "Validation" in the README)."""
        if when - self.last_crop.get(name, datetime.min) < CROP_EVERY:
            return
        self.last_crop[name] = when
        folder = CROPS / when.date().isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        crop = crop.convert("RGB")
        crop.thumbnail((320, 320))
        crop.save(folder / f"{when:%H%M%S}_{file_safe(name)}_{prob:.2f}.jpg", quality=85)

    # --- hourly summary ----------------------------------------------------
    def _load_hour(self):
        try:
            return json.loads(HOUR_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._new_hour(datetime.now())

    @staticmethod
    def _new_hour(when: datetime):
        return {"date": when.date().isoformat(), "hour": when.hour, "analyzed": 0, "dark": 0, "species": {}}

    def _roll_hour(self, when: datetime):
        h = self.hour
        if (h["date"], h["hour"]) == (when.date().isoformat(), when.hour):
            return
        if h["analyzed"]:
            rows = [[h["date"], h["hour"], h["analyzed"], h["dark"], name, seen, most]
                    for name, (seen, most) in sorted(h["species"].items())]
            self._append(HOURLY_CSV, HOURLY_FIELDS, rows or [[h["date"], h["hour"], h["analyzed"], h["dark"], "", 0, 0]])
        if self.publish and h["date"] != when.date().isoformat():
            # New day: push yesterday's stats to the public README, at idle priority, without waiting.
            subprocess.Popen([sys.executable, str(ROOT / "publish_results.py")], cwd=ROOT,
                             creationflags=0x08000000 | 0x40)  # CREATE_NO_WINDOW | IDLE_PRIORITY_CLASS
            log.info("publishing daily results")
        self.hour = self._new_hour(when)
        if when - self.last_prune > timedelta(hours=6):
            self.last_prune = when
            self._prune_crops(when)

    def _save_hour(self):
        HOUR_STATE.write_text(json.dumps(self.hour), encoding="utf-8")

    @staticmethod
    def _prune_crops(now: datetime):
        cutoff = (now - timedelta(days=KEEP_CROPS_DAYS)).date().isoformat()
        for folder in CROPS.glob("*"):
            if folder.is_dir() and folder.name < cutoff:
                shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _append(path: Path, fields, rows):
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new:
                writer.writerow(fields)
            writer.writerows(rows)


def enter_efficiency_mode():
    """Windows Efficiency mode (idle priority + EcoQoS) for this process. LiveCams sets it on
    the process it starts, but a venv's pythonw.exe is only a stub that runs the real
    interpreter as a child, and EcoQoS isn't inherited."""

    class PowerThrottlingState(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]

    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # a 64-bit pseudo-handle, not an int
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    me = kernel32.GetCurrentProcess()
    kernel32.SetPriorityClass(me, 0x40)  # IDLE_PRIORITY_CLASS
    state = PowerThrottlingState(1, 1, 1)  # PROCESS_POWER_THROTTLING_EXECUTION_SPEED on
    kernel32.SetProcessInformation(me, 4, ctypes.byref(state), ctypes.sizeof(state))  # ProcessPowerThrottling


def main():
    enter_efficiency_mode()
    (LIVECAMS / "logs").mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LIVECAMS / "logs" / "tracker.log", maxBytes=1_000_000,
                                                   backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    tracker = Tracker(publish="--publish" in sys.argv)
    last_mtime = None
    while True:
        try:
            mtime = FRAME.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                if time.time() - mtime < STALE_SECONDS:  # older = the wallpaper isn't streaming
                    data = FRAME.read_bytes()  # read at once: LiveCams replaces the file every few seconds
                    tracker.process(Image.open(io.BytesIO(data)).convert("RGB"), datetime.fromtimestamp(mtime))
            tracker.unload_if_idle()
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("frame failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
