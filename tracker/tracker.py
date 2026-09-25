"""Animal tracker for the Under Scripps Pier cam.

Whenever the LiveCams wallpaper saves a new frame (frames/underwater/latest.jpg,
about every 10 s while the stream plays), this finds fish with the Community Fish
Detector, names the ones big enough to identify with BioCLIP 2, looks at whatever
else moved for animals the fish detector won't box (octopus, crabs, jellies, sea
lions, divers...), and appends what it saw to data/sightings.csv. The camera never
moves, so anything that doesn't move (pilings, the hanging rope, the growth on them)
is ignored. Sightings it isn't sure about are saved to data/review/pending for a
person to approve or correct.

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
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import openvino as ov
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
MIN_NAME_PX = 80            # fish smaller than this (longest side, 1080p frame) are too small to tell apart
SPECIES_MIN_PROB = 0.60     # below this a detected fish is logged as unidentified
NEGATIVE_MIN_PROB = 0.50    # a detection this sure to be murk/kelp/piling is dropped
SCENE_MIN_PROB = 0.70       # a non-fish/big animal must beat every label by this much
SCENE_SURE_PROB = 0.90      # in a small moving area it must reach this, or show up again soon:
SCENE_REPEAT = timedelta(seconds=30)  # a real lobster or octopus stays; a flicker of fish at a piling doesn't
BIG_BOX_FRACTION = 0.25     # a detection this big is the animal the whole-frame check found
SCHOOL_MIN = 5              # this many of one kind in a snapshot is logged once, as "<kind> (school)"
MOTION_SCALE = 8            # motion is measured on a 1/8-size grayscale copy of the frame
MOTION_ALPHA = 0.1          # background update per frame (remembers roughly the last 100 s)
MOTION_MIN_DIFF = 10        # gray levels of change that count as movement
MOTION_WARMUP = 3           # frames needed to learn the background after a start or a gap
BOX_MIN_MOTION = 0.10       # a detection needs this share of its area to have moved
BLOB_MIN_PX = 90            # moving areas smaller than this are small fish, not a new kind of animal
MAX_BLOBS = 4               # biggest moving areas checked per frame
SCENE_MIN_MOTION = 0.15     # the whole frame is checked for a big animal when this much of it moved
REVIEW_FISH_PROB = 0.25     # an unidentified (big enough) fish whose best guess is at least this goes to review
REVIEW_SCENE_PROB = 0.40    # a non-fish/big-animal guess between this and SCENE_MIN_PROB goes to review
REVIEW_EVERY = timedelta(minutes=10)  # at most one "not sure" picture per best guess per 10 min...
REVIEW_PER_HOUR = 6         # ...and at most this many per hour
CHECK_EVERY = timedelta(hours=1)      # a sample of confident names goes to review too: one per animal per hour...
CHECK_PER_HOUR = 4          # ...at most this many per hour. The answers become the published accuracy.
REVIEW_MAX_PENDING = 300    # stop adding review pictures while this many wait
KEEP_PENDING_DAYS = 14      # unreviewed pictures older than this are deleted
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
SMALL_FISH = {"common": "small fish", "scientific": "", "category": "fish"}

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
        # The fish detector only finds fish, so its boxes are only ever given fish names (or a
        # "not an animal" label): a blurry fish can't come out as an octopus or a jellyfish.
        self.fish_idx = np.array([i for i, l in enumerate(self.labels)
                                  if l["negative"] or l["category"] in ("fish", "shark/ray")])
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

    def probabilities(self, embedding: np.ndarray, subset=None) -> np.ndarray:
        """Softmax over all labels, or over labels[subset] (then index i means labels[subset[i]])."""
        emb = self.label_emb if subset is None else self.label_emb[subset]
        logits = self.logit_scale * emb @ embedding
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


def rough(n: int) -> int:
    """A school's size, rounded so it doesn't look more precise than it is."""
    return n if n < 20 else int(round(n, -1)) if n < 100 else int(round(n / 50) * 50)


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


def top_guesses(labels, probs, subset=None, n=3):
    """The n most likely animals (never the "not an animal" labels), for the review queue."""
    index = np.arange(len(probs)) if subset is None else subset
    order = [k for k in np.argsort(-probs) if not labels[index[k]]["negative"]][:n]
    return [{"common": labels[index[k]]["common"], "scientific": labels[index[k]]["scientific"],
             "category": labels[index[k]]["category"], "prob": round(float(probs[k]), 3)} for k in order]


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
    """Night: too dark, no detail, or the purple-grey sensor noise this camera shows at night.
    In daylight the water here is green, so the green channel leads."""
    rgb = np.asarray(img.convert("RGB").resize((64, 36), Image.BOX), np.float32)
    gray = rgb.mean(axis=2)
    r, g, b = rgb.reshape(-1, 3).mean(axis=0)
    return gray.mean() < DARK_MEAN or gray.std() < DARK_DETAIL or (g < b and g < r + 10)


class Motion:
    """What changed compared with a slowly updated background. The camera never moves, so the
    pilings, the hanging rope and the growth on them stay put, and animals are what move."""

    def __init__(self):
        self.background = None
        self.mask = None
        self.frames = 0
        self.last = None

    def update(self, img: Image.Image, when: datetime) -> bool:
        """Feeds a frame in; returns False while the background is still being learned."""
        small = np.asarray(img.convert("L").resize(
            (img.width // MOTION_SCALE, img.height // MOTION_SCALE), Image.BOX), np.float32)
        if (self.background is None or self.background.shape != small.shape
                or when - self.last > timedelta(minutes=2)):
            self.background, self.frames = small.copy(), 0  # new start, or the scene may have changed
        diff = np.abs(small - self.background)
        self.mask = diff > max(MOTION_MIN_DIFF, 4 * float(np.median(diff)))  # ignore overall light flicker
        self.background += MOTION_ALPHA * (small - self.background)
        self.frames += 1
        self.last = when
        return self.frames > MOTION_WARMUP

    def moving_share(self) -> float:
        return float(self.mask.mean())

    def fraction(self, box) -> float:
        """Share of a full-size box that moved."""
        x0, y0, x1, y1 = (int(round(v / MOTION_SCALE)) for v in box[:4])
        region = self.mask[max(y0, 0):max(y1, y0 + 1), max(x0, 0):max(x1, x0 + 1)]
        return float(region.mean()) if region.size else 0.0

    def blobs(self, min_px):
        """Full-size boxes around connected moving areas at least min_px on their longest side,
        biggest first (nearby specks are merged first, so a close animal is one area)."""
        grown = np.asarray(Image.fromarray(self.mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(3))) > 0
        found = [(area, box) for area, box in components(grown) if max(box[2] - box[0], box[3] - box[1]) >= min_px]
        return [box for _, box in sorted(found, reverse=True)]

    def small_movers(self, max_px) -> int:
        """How many separate small moving specks there are: a rough head count for a school of fish
        too small for the detector (it misses most fish under ~20 px)."""
        return sum(1 for _, box in components(self.mask) if max(box[2] - box[0], box[3] - box[1]) < max_px)


def components(mask: np.ndarray):
    """(area, full-size box) for each 4-connected region of a motion mask."""
    h, w = mask.shape
    seen = np.zeros_like(mask)
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        seen[y, x] = True
        stack, y0, y1, x0, x1, area = [(y, x)], y, y, x, x, 0
        while stack:
            cy, cx = stack.pop()
            area += 1
            y0, y1, x0, x1 = min(y0, cy), max(y1, cy), min(x0, cx), max(x1, cx)
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        yield area, (x0 * MOTION_SCALE, y0 * MOTION_SCALE, (x1 + 1) * MOTION_SCALE, (y1 + 1) * MOTION_SCALE)


class Tracker:
    def __init__(self, publish=False):
        self.publish = publish
        self._models = None
        self.last_model_use = datetime.now()
        DATA.mkdir(parents=True, exist_ok=True)
        self.hour = self._load_hour()
        self.last_prune = datetime.min
        self.last_crop = {}
        self.last_review = {"uncertain": {}, "check": {}}
        self.review_times = {"uncertain": deque(), "check": deque()}
        self.motion = Motion()
        self.scene_first_seen = {}

    def process(self, img: Image.Image, when: datetime):
        self._roll_hour(when)
        self.hour["analyzed"] += 1
        if is_dark(img):
            self.hour["dark"] += 1
            self._save_hour()
            return []

        started = time.perf_counter()
        if not self.motion.update(img, when):
            self._save_hour()
            return []  # still learning what the empty scene looks like
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

        # Fish boxes, keeping only those where something moved: the detector sometimes boxes the
        # pilings or the rope hanging from the pier, and those never move.
        fish_boxes = [b for b in m.detect(img)
                      if min(b[2] - b[0], b[3] - b[1]) >= MIN_CROP_PX and self.motion.fraction(b) >= BOX_MIN_MOTION]

        # 1. Everything that isn't a fish the detector found (octopus, crabs, lobsters, jellies, sea
        #    lions, rays, divers...): look at what moved. Each moving area bigger than a small fish,
        #    and the whole frame when a lot of it moved, is compared against every label. It only
        #    counts when a "scene" animal wins with at least SCENE_MIN_PROB; a close call goes to
        #    the review queue.
        full = (0, 0, img.width, img.height)
        views = [b for b in self.motion.blobs(BLOB_MIN_PX) if all(iou(b, f) < 0.3 for f in fish_boxes)]
        views = views[:MAX_BLOBS] + ([full] if self.motion.moving_share() >= SCENE_MIN_MOTION else [])
        whole_frame_animal = None
        for box in views:
            view = img if box == full else square_crop(img, box)
            emb = m.embed(view)
            probs = m.probabilities(emb)
            best = int(probs.argmax())
            label = m.labels[best]
            if not label["scene"] or label["negative"]:
                continue
            name, prob = label["common"], float(probs[best])
            if box != full and SCENE_MIN_PROB <= prob < SCENE_SURE_PROB:
                first = self.scene_first_seen.get(name)
                if not first or when - first > SCENE_REPEAT:
                    self.scene_first_seen[name] = when  # wait for it to show up again
                    self._queue_review(view, img, box, top_guesses(m.labels, probs), when, embedding=emb)
                    continue
            if prob >= SCENE_MIN_PROB:
                if name not in found:
                    self._save_crop(view, when, label["common"], float(probs[best]))
                    self._queue_review(view, img, box, top_guesses(m.labels, probs), when, logged_as=label["common"],
                                       embedding=emb)
                    add(label, float(probs[best]))
                if box == full:
                    whole_frame_animal = label
            elif probs[best] >= REVIEW_SCENE_PROB:
                self._queue_review(view, img, box, top_guesses(m.labels, probs), when, embedding=emb)

        # 2. Fish. Big enough: named from the fish labels only, or logged as unidentified (and maybe
        #    sent to review) when unsure. Too small to tell apart: see 3.
        frame_area = img.width * img.height
        small_scores = []
        for box in fish_boxes:
            w, h = box[2] - box[0], box[3] - box[1]
            if whole_frame_animal and w * h > BIG_BOX_FRACTION * frame_area:
                continue  # the big animal the whole-frame check already named
            if max(w, h) < MIN_NAME_PX:
                small_scores.append(float(box[4]))
                continue
            crop = square_crop(img, box)
            emb = m.embed(crop)
            probs = m.probabilities(emb, m.fish_idx)
            best = int(probs.argmax())
            label = m.labels[m.fish_idx[best]]
            if label["negative"] and probs[best] >= NEGATIVE_MIN_PROB:
                continue  # murk, kelp or piling, not an animal
            if label["negative"] or probs[best] < SPECIES_MIN_PROB:
                guesses = top_guesses(m.labels, probs, m.fish_idx)
                if guesses and guesses[0]["prob"] >= REVIEW_FISH_PROB:
                    self._queue_review(crop, img, box[:4], guesses, when, embedding=emb)
                label, prob = UNIDENTIFIED, float(box[4])  # a fish, but not sure which: don't guess
            else:
                prob = float(probs[best])
                self._queue_review(crop, img, box[:4], top_guesses(m.labels, probs, m.fish_idx), when,
                                   logged_as=label["common"], embedding=emb)
            add(label, prob)
            self._save_crop(crop, when, label["common"], prob)

        # 3. Small fish aren't tracked one by one: a school is one sighting a snapshot, with a rough
        #    size. The detector only boxes a few of them, so the size comes from counting the small
        #    moving specks, but only once the detector confirms there are fish at all.
        if small_scores:
            size = max(len(small_scores), self.motion.small_movers(MIN_NAME_PX))
            add(SMALL_FISH, max(small_scores), rough(size) if size >= SCHOOL_MIN else len(small_scores))

        # Five or more of one kind in a snapshot is logged as that kind's school.
        return [Sighting(l["common"] + (" (school)" if n >= SCHOOL_MIN else ""), l["scientific"], l["category"], n, p)
                for n, p, l in found.values()]

    def _queue_review(self, view: Image.Image, frame: Image.Image, box, guesses, when: datetime, logged_as=None,
                      embedding=None):
        """Saves a sighting for a person to look at in the LiveCams review window: either one the
        tracker wasn't sure about ("uncertain"), or, with logged_as, a sample of a name it did
        log ("check"), so the published stats can say how often its names are right."""
        kind = "check" if logged_as else "uncertain"
        name = logged_as or guesses[0]["common"]
        every, per_hour = (CHECK_EVERY, CHECK_PER_HOUR) if logged_as else (REVIEW_EVERY, REVIEW_PER_HOUR)
        if when - self.last_review[kind].get(name, datetime.min) < every:
            return
        recent = self.review_times[kind]
        while recent and when - recent[0] > timedelta(hours=1):
            recent.popleft()
        if len(recent) >= per_hour:
            return
        REVIEW.mkdir(parents=True, exist_ok=True)
        if sum(1 for _ in REVIEW.glob("*.json")) >= REVIEW_MAX_PENDING:
            return
        self.last_review[kind][name] = when
        recent.append(when)
        stem = REVIEW / f"{when:%Y%m%d-%H%M%S}_{file_safe(name)}"
        review_card(view, frame, box, guesses, when).save(stem.with_suffix(".jpg"), quality=88)
        stem.with_suffix(".json").write_text(json.dumps({
            "kind": kind,
            "logged_as": logged_as or "",
            "taken_at": when.isoformat(timespec="seconds"),
            "box": [round(float(v)) for v in box],
            "guesses": guesses,
            # BioCLIP's image embedding: with the person's answer, this is a ready-made training
            # example for a classifier fitted to this camera (see "Where this could go" in the README).
            "embedding": [] if embedding is None else [round(float(v), 4) for v in embedding],
        }), encoding="utf-8")
        log.info("queued for review (%s): %s (%.0f%%)", kind, name, 100 * guesses[0]["prob"])

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
        if h["date"] != when.date().isoformat():
            # New day: fetch the pier's conditions, and publish yesterday's stats to the public README
            # if enabled (that fetches the conditions too). Idle priority, no waiting.
            script = "publish_results.py" if self.publish else "environment.py"
            subprocess.Popen([sys.executable, str(ROOT / script)], cwd=ROOT,
                             creationflags=0x08000000 | 0x40)  # CREATE_NO_WINDOW | IDLE_PRIORITY_CLASS
            log.info("new day: running %s", script)
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
        stale = (now - timedelta(days=KEEP_PENDING_DAYS)).strftime("%Y%m%d")
        for f in REVIEW.glob("*"):
            if f.name[:8] < stale:
                f.unlink(missing_ok=True)

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
