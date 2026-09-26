"""Animal tracker for the Under Scripps Pier cam.

Whenever the LiveCams wallpaper saves a new frame (frames/underwater/latest.jpg,
about every 10 s while the stream plays), this finds fish with the Community Fish
Detector, names the ones big enough to identify with BioCLIP 2, looks at whatever
else moved for animals the fish detector won't box (octopus, crabs, jellies, sea
lions, divers...), and records what it saw in the database (data/piertracker.db). The camera never
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
import os
import shutil
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import openvino as ov
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import db

ROOT = Path(__file__).resolve().parent
LIVECAMS = ROOT.parent
FRAME = LIVECAMS / "frames" / "underwater" / "latest.jpg"
BURST_FLAG = FRAME.parent / "burst_until"  # LiveCams captures faster until this Unix time
# The last few minutes of daylight frames, so "did it see that?" can be answered afterwards: with a
# .json next to a frame listing what was ignored there as fixed structure.
RECENT = FRAME.parent / "recent"
# What the tracker sees in the latest frame (boxes, names, logged or not sure), for the community
# viewer (community/server.py). Rewritten every frame.
LIVE = FRAME.parent / "live.json"
KEEP_RECENT = timedelta(minutes=10)
MODELS = ROOT / "models"
DATA = LIVECAMS / "data"
HOUR_STATE = DATA / ".current_hour.json"
STOP_FILE = LIVECAMS / "tracker.stop"  # LiveCams creates this to ask the tracker to exit cleanly
CROPS = DATA / "crops"
REVIEW = DATA / "review" / "pending"
CAMERA_MODEL = MODELS / "camera_classifier.npz"
REFERENCE_PROBE = MODELS / "reference_probe.npz"  # tracker/ml/reference_photos.py; shadow mode for now
NOT_AN_ANIMAL = "not an animal"
BANK = DATA / "frame_bank"
BACKGROUND_IMAGE = DATA / "background.png"
FIXTURE_GALLERY = DATA / "fixture_gallery.npz"
REVIEWED = {"rejected": DATA / "review" / "rejected", "approved": DATA / "review" / "approved"}

POLL_SECONDS = 1
REGULAR_SECONDS = 9          # frames closer together than this are burst frames (tracking only, not stats)
# Following a fish across frames: averaging its ID over a visit was ~5 points more accurate in a test
# (README). When a fish big enough to name is in view, LiveCams is asked for a frame every ~3 s.
TRACK_GAP = timedelta(seconds=12)   # a fish unseen this long has left: its visit is recorded
TRACK_MIN_DISTANCE = 150            # px: how far a fish may move between frames and still be the same one
SCENE_VISIT_GAP = timedelta(minutes=5)  # a lobster, turtle or seal seen again within this is the same visit
BURST_SECONDS = 20
BURST_BUDGET = timedelta(minutes=10)  # at most this much burst time per hour
STALE_SECONDS = 60          # a frame older than this means the wallpaper isn't streaming
DARK_MEAN = 20              # mean brightness (0-255) below which a frame is "night"
DARK_DETAIL = 4.0           # detail left after blurring; night sensor noise averages away to ~0
DETECT_THRESHOLD = 0.35     # fish detector confidence
MIN_NAME_PX = 80            # fish smaller than this (longest side, 1080p frame) are too small to tell apart
# Cutoffs measured on photos of known species degraded to look like this camera (README,
# "Validation"): at 0.85 / 0.90 about 80% of the names logged are right; at 0.6 it was ~64%.
SPECIES_MIN_PROB = 0.85     # a species name needs this probability...
GROUP_MIN_PROB = 0.90       # ...a look-alike group this much (summed over its members)...
# ...and in hazy water, where naming accuracy halves long before counting suffers, no species at all:
SPECIES_MIN_PROB_POOR = float("inf")
GROUP_MIN_PROB_POOR = 0.95
# Visibility = fine detail left in the frame (mean |frame - blurred frame| at 240x135). Clear water
# today reads 2.1-3.1. With simulated murk, naming fell from 92% right to 50% by ~1.1 and detection
# collapsed below ~0.2 (README, "Validation").
VISIBILITY_POOR = 1.4
VISIBILITY_MIN = 0.2
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
# Fixed things that sway in the surge (growth hanging off the crossbeam, growth on a piling) move
# enough to pass the motion checks, and got named "leopard shark" or "green sea turtle 100%". They
# are part of the long-term background; a passing animal isn't. See Background.
BACKGROUND_EVERY = timedelta(minutes=5)  # one clear daylight frame every 5 minutes...
BACKGROUND_FRAMES = 12                   # ...over the last hour; the background is their median
BACKGROUND_MIN_FRAMES = 4
# Structure, not an animal, when a crop looks like a person-confirmed fixture at the same place
# (FixtureGallery) and like the long-term background: (gallery >=, background >=). Checked on
# 2026-09-25's labeled review pictures, each fixture against the others: 21 of 22 fixtures, no real
# fish (0 of 59), and the lobster in its crevice kept. The kelp bass by the round growth stays below.
FIXTURE_LOOKS = ((0.75, -1.0), (0.65, 0.80), (0.60, 0.85))
FIXTURE_SURE_CORR = 0.90    # at a confirmed fixture's place, this alike to the background: skip the model...
SHORTCUT_MAX_PX = 500       # ...for crops smaller than this; a big one (an antenna sweeping past the lens) gets a look
# Where nobody has confirmed what's there, a named crop this alike to the background could be swaying
# structure or an animal sitting still (a lobster in its crevice): it isn't logged, a person decides
# (review queue). Real fish passing by scored at most 0.835 on 2026-09-25.
SUSPECT_CORR = 0.85
ANIMAL_LOOKS = 0.75         # this alike to a confirmed animal at the same place: trusted, logged
# This alike to a picture a person confirmed as a non-fish animal, at the same place: logged as that
# animal whatever the model says (the lobster's antenna, which it calls a stingray). On 2026-09-25's
# review pictures: ~14 of 17 antenna pictures, none of 155 others. Fish names are left to the
# camera-trained classifier, which is cross-checked on many answers.
ANIMAL_NAME_SIM = 0.82
DECISIONS = DATA / "review" / "decisions.csv"
GALLERY_MAX = 3000
CAMERA_MIN_PROB = 0.70      # the camera-trained classifier's answer is used when it's at least this sure
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
# A frame bank for training a detector on this camera later: self-supervised pretraining works
# best on lots of unlabeled frames from the very same camera (see README). ~20 MB a day.
BANK_EVERY = timedelta(minutes=20)
BANK_INTERESTING_EVERY = timedelta(minutes=5)  # frames with a named animal are worth more
KEEP_BANK_DAYS = 90
UNLOAD_AFTER = timedelta(minutes=10)  # free the models' ~1 GB at night and while the cams are paused
CROP_EVERY = timedelta(minutes=10)  # keep one sample crop per animal type per 10 min, for review
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)

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
    method: str = "zero-shot"  # zero-shot | camera-trained | detector only


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
        self.embedding_model = meta.get("model", "hf-hub:imageomics/bioclip-2")
        self.camera, self.camera_mtime = None, None
        self.reference, self.reference_mtime = None, None
        # The fish detector only finds fish, so its boxes are only ever given fish names (or a
        # "not an animal" label): a blurry fish can't come out as an octopus or a jellyfish.
        self.fish_idx = np.array([i for i, l in enumerate(self.labels)
                                  if l["negative"] or l["category"] in ("fish", "shark/ray")])
        # Look-alike groups from species.json ("silversides & sardines", "jellyfish"...).
        spec = json.loads((ROOT / "species.json").read_text(encoding="utf-8"))
        self.group_of = {sp["common"]: sp["group"] for sp in spec["species"] if sp.get("group")}
        members = defaultdict(list)
        for sp in spec["species"]:
            if sp.get("group"):
                members[sp["group"]].append(sp)
        self.groups = {g: {"common": g, "scientific": "", "category": ms[0]["category"], "negative": False,
                           "scene": any(m.get("scene") for m in ms), "group": True} for g, ms in members.items()}
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

    def name(self, probs: np.ndarray, subset, min_prob: float, group_min: float = None):
        """(label, probability) for the most likely animal if it reaches min_prob; otherwise for its
        look-alike group when the model spreads its confidence over the group's members (a smelt it
        can't split between topsmelt, jacksmelt, anchovy and sardine is still clearly one of them);
        otherwise (None, best probability). "Not an animal" labels are never returned."""
        index = np.arange(len(probs)) if subset is None else subset
        animals = [k for k in range(len(probs)) if not self.labels[index[k]]["negative"]]
        best = max(animals, key=lambda k: probs[k])
        if probs[best] >= min_prob:
            return self.labels[index[best]], float(probs[best])
        totals = defaultdict(float)
        for k in animals:
            group = self.group_of.get(self.labels[index[k]]["common"])
            if group:
                totals[group] += float(probs[k])
        if totals:
            group, total = max(totals.items(), key=lambda kv: kv[1])
            if total >= (min_prob if group_min is None else group_min):
                return self.groups[group], total
        return None, float(probs[best])

    def camera_name(self, embedding: np.ndarray):
        """The camera-trained classifier's answer (ml/train_classifier.py), if one is switched on and
        it's sure: (label, probability), or ("not an animal", probability). None otherwise."""
        mtime = CAMERA_MODEL.stat().st_mtime if CAMERA_MODEL.exists() else None
        if mtime != self.camera_mtime:  # (re)load after nightly retraining
            self.camera_mtime, self.camera = mtime, None
            if mtime:
                model = np.load(CAMERA_MODEL, allow_pickle=False)
                if bool(model["enabled"]) and str(model["embedding_model"]) == self.embedding_model:
                    self.camera = model
                    log.info("camera-trained classifier on (%d animals)", len(model["classes"]))
        if self.camera is None:
            return None
        logits = self.camera["coef"] @ embedding + self.camera["intercept"]
        p = np.exp(logits - logits.max())
        p /= p.sum()
        best = int(p.argmax())
        if p[best] < CAMERA_MIN_PROB:
            return None
        name = str(self.camera["classes"][best])
        if name == "not an animal":
            return name, float(p[best])
        label = self.groups.get(name) or next((l for l in self.labels if l["common"] == name), None)
        return (label, float(p[best])) if label else None

    def label_named(self, name: str):
        return next((l for l in self.labels if l["common"] == name and not l["negative"]), None)

    def reference_opinion(self, embedding: np.ndarray, subset=None):
        """Shadow mode: what the classifier trained on reference photos would call this, as
        {"name", "prob", "not_animal"}, or None when it isn't there. Not used for logging yet: stored
        on review pictures and compared with people's answers (ml/reference_photos.py evaluate)."""
        mtime = REFERENCE_PROBE.stat().st_mtime if REFERENCE_PROBE.exists() else None
        if mtime != self.reference_mtime:  # (re)load after retraining
            self.reference_mtime, self.reference = mtime, None
            if mtime:
                with np.load(REFERENCE_PROBE, allow_pickle=False) as d:
                    if str(d["embedding_model"]) == self.embedding_model:
                        self.reference = {k: d[k] for k in d.files}
        if self.reference is None:
            return None
        r = self.reference
        index = np.arange(len(self.labels)) if subset is None else np.asarray(subset)
        names = [self.labels[i]["common"] for i in index]
        negative = np.array([self.labels[i]["negative"] for i in index])
        p = combine_reference(self.probabilities(embedding, subset), names, negative,
                              reference_probs(r, embedding), [str(c) for c in r["classes"]], float(r["w"]))
        animals = np.where(~negative)[0]
        best = animals[int(p[animals].argmax())]
        return {"name": names[best], "prob": round(float(p[best]), 3), "not_animal": round(float(p[negative].sum()), 3)}

    def probabilities(self, embedding: np.ndarray, subset=None) -> np.ndarray:
        """Softmax over all labels, or over labels[subset] (then index i means labels[subset[i]])."""
        emb = self.label_emb if subset is None else self.label_emb[subset]
        logits = self.logit_scale * emb @ embedding
        p = np.exp(logits - logits.max())
        return p / p.sum()


def reference_probs(probe, embedding: np.ndarray) -> np.ndarray:
    """The reference-photo classifier's probabilities over its own classes (species + not an animal)."""
    e = np.asarray(embedding, np.float64)
    z = probe["coef"] @ (e / np.linalg.norm(e)) + probe["intercept"]
    p = np.exp(z - z.max())
    return p / p.sum()


def combine_reference(zero_shot, names, negative, probe_probs, probe_classes, w):
    """Mixes the reference-photo classifier into zero-shot probabilities over the same labels. How likely
    it's an animal at all comes from zero-shot, lowered by the classifier's own "not an animal" vote
    (trained on this camera's background); which animal is zero-shot's share and the classifier's,
    mixed as zero_shot^(1-w) * classifier^w for the species the classifier knows."""
    zs = np.asarray(zero_shot, np.float64)
    animal = ~np.asarray(negative)
    zs_animal = float(zs[animal].sum())
    cond = zs[animal] / max(zs_animal, 1e-12)
    index = {c: i for i, c in enumerate(probe_classes)}
    q = cond.copy()
    for j, name in enumerate(n for n, a in zip(names, animal) if a):
        if name in index:
            q[j] = probe_probs[index[name]]
    mix = np.exp((1 - w) * np.log(cond + 1e-12) + w * np.log(q + 1e-12))
    mix /= mix.sum()
    share = zs_animal * (1 - probe_probs[index[NOT_AN_ANIMAL]]) if NOT_AN_ANIMAL in index else zs_animal
    out = zs.copy()
    out[~animal] = zs[~animal] * (1 - share) / max(1 - zs_animal, 1e-12)
    out[animal] = share * mix
    return out


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


def square_box(img: Image.Image, box, pad=0.15):
    """The box, padded and made square so the classifier doesn't see a squashed fish. Near an
    edge the square slides inward rather than filling with black."""
    x0, y0, x1, y1 = box[:4]
    side = min(max(x1 - x0, y1 - y0) * (1 + 2 * pad), img.width, img.height)
    left = min(max((x0 + x1 - side) / 2, 0), img.width - side)
    top = min(max((y0 + y1 - side) / 2, 0), img.height - side)
    return int(left), int(top), int(left + side), int(top + side)


def square_crop(img: Image.Image, box, pad=0.15):
    return img.crop(square_box(img, box, pad))


def standardize(a: np.ndarray) -> np.ndarray:
    return (a - a.mean()) / (a.std() + 1e-6)


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


def visibility(img: Image.Image) -> float:
    """How much fine detail the frame shows: edges of pilings, fish, growth. Murky water washes
    them out, so this falls as turbidity rises."""
    g = np.asarray(img.convert("L").resize((240, 135), Image.BOX), np.float32)
    blurred = np.asarray(Image.fromarray(g.astype(np.uint8)).filter(ImageFilter.GaussianBlur(3)), np.float32)
    return float(np.mean(np.abs(g - blurred)))


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

    def peek(self, img: Image.Image):
        """Motion mask for a burst frame, without updating the background (it's tuned for ~10 s steps)."""
        if self.background is None:
            return
        small = np.asarray(img.convert("L").resize(
            (img.width // MOTION_SCALE, img.height // MOTION_SCALE), Image.BOX), np.float32)
        diff = np.abs(small - self.background)
        self.mask = diff > max(MOTION_MIN_DIFF, 4 * float(np.median(diff)))

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


@dataclass
class Track:
    """One fish followed across frames. Its name comes from the average of all its looks."""
    first_seen: datetime
    last_seen: datetime
    box: tuple
    probs_sum: np.ndarray
    looks: int = 1
    name: str = ""
    confidence: float = 0.0

    def near(self, box) -> bool:
        cx, cy = (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2
        bx, by = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        reach = max(TRACK_MIN_DISTANCE, 1.5 * max(box[2] - box[0], box[3] - box[1]))
        return (cx - bx) ** 2 + (cy - by) ** 2 <= reach ** 2


class Background:
    """The scene's long-term look: the median of one clear daylight frame every 5 minutes over the
    last hour (grey, half size). Pilings and whatever grows or hangs on them are in it; a passing
    animal isn't, since it's somewhere else in most of the samples. So a crop that looks like the
    same spot of this image is structure moving in the surge, not an animal. On the 68 review
    pictures from 2026-09-25 this caught 16 of 17 such fixtures and none of 51 real fish (README,
    "Fixed things that sway"). An animal that stays put for half an hour or more becomes part of
    the background too; its visit is logged before that."""

    def __init__(self):
        self.samples = deque(maxlen=BACKGROUND_FRAMES)
        self.last = datetime.min
        self.median = None
        try:
            self.median = Image.open(BACKGROUND_IMAGE).convert("L")  # from before a restart
        except OSError:
            pass

    def add(self, img: Image.Image, when: datetime):
        if when - self.last < BACKGROUND_EVERY:
            return
        self.last = when
        small = img.convert("L").resize((img.width // 2, img.height // 2), Image.BOX)
        self.samples.append(np.asarray(small, np.uint8))
        if len(self.samples) >= BACKGROUND_MIN_FRAMES:
            self.median = Image.fromarray(np.median(np.stack(self.samples), axis=0).astype(np.uint8))
            self.median.save(BACKGROUND_IMAGE)

    def correlation(self, img: Image.Image, box) -> float:
        """How much the square the classifier would see looks like the background there (-1..1),
        allowing two pixels (of 32) of sway either way. -1 until there is a background."""
        if self.median is None:
            return -1.0
        x0, y0, x1, y1 = square_box(img, box)
        sx, sy = self.median.width / img.width, self.median.height / img.height
        now = np.asarray(img.crop((x0, y0, x1, y1)).convert("L").resize((36, 36), Image.BOX), np.float32)
        bg = np.asarray(self.median.crop((x0 * sx, y0 * sy, x1 * sx, y1 * sy)).resize((36, 36), Image.BOX), np.float32)
        now = standardize(now[2:34, 2:34])
        return max(float((now * standardize(bg[y:y + 32, x:x + 32])).mean()) for y in range(5) for x in range(5))


def overlap(a, b) -> float:
    """Intersection over the smaller box's area."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / small if small > 0 else 0.0


class FixtureGallery:
    """Person-confirmed structure and animals, with where they were: the classifier's embeddings of
    review pictures marked "not an animal" (fixtures) or approved as an animal. The hanging growth and
    the round growth on the piling fool the model ("leopard shark", "green sea turtle 100%"), and the
    long-term background alone can't tell them from an animal that sits still: a lobster in its
    crevice becomes part of the background too. What a person has confirmed can. A crop is structure
    when it looks like a confirmed fixture at the same place (FIXTURE_LOOKS) more than like any
    confirmed animal there. Every review answer adds to it."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.boxes, self.embs, self.animal, self.names, self.sources = [], [], [], [], set()
        try:
            with np.load(FIXTURE_GALLERY, allow_pickle=False) as data:  # closed after: Windows can't replace an open file
                if str(data["model"]) == model_name and "names" in data.files:  # older files: rebuilt from the answers
                    self.boxes = [tuple(float(v) for v in b) for b in data["boxes"]]
                    self.embs = [np.array(e) for e in data["embs"]]
                    self.animal = [bool(a) for a in data["animal"]]
                    self.names = [str(n) for n in data["names"]]
                    self.sources = set(str(x) for x in data["sources"])
        except (OSError, KeyError, ValueError):
            pass
        self.add_reviewed()

    def add(self, box, emb, animal: bool, source: str, name: str = ""):
        if source in self.sources:
            return
        self.sources.add(source)
        self.boxes.append(tuple(float(v) for v in box[:4]))
        self.embs.append(np.asarray(emb, np.float32) / np.linalg.norm(emb))
        self.animal.append(animal)
        self.names.append(name if animal else "")
        if len(self.embs) > GALLERY_MAX:
            del self.boxes[0], self.embs[0], self.animal[0], self.names[0]

    def add_reviewed(self):
        """New review answers: "not an animal" pictures are fixtures, approved ones animals (with the name
        given in the review window)."""
        before = len(self.sources)
        answers = {}
        try:
            with DECISIONS.open(encoding="utf-8") as f:
                answers = {r["image"]: r["common_name"] for r in csv.DictReader(f) if r["decision"] == "approved"}
        except OSError:
            pass
        for decision, folder in REVIEWED.items():
            for card in folder.glob("*.json") if folder.exists() else []:
                if card.name in self.sources:
                    continue
                try:
                    d = json.loads(card.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if d.get("embedding_model") == self.model_name and d.get("embedding") and d.get("box"):
                    self.add(square_box(Image.new("L", (1920, 1080)), d["box"]), d["embedding"],
                             animal=decision == "approved", source=card.name,
                             name=answers.get(card.with_suffix(".jpg").name, ""))
        if len(self.sources) != before:
            self.save()

    def _near(self, img, box):
        """Gallery entries covering more than 30% of this crop's square (a big crop that only clips a
        fixture's place isn't at that place)."""
        sq = square_box(img, box)
        area = (sq[2] - sq[0]) * (sq[3] - sq[1])
        near = []
        for i, b in enumerate(self.boxes):
            ix = max(0.0, min(sq[2], b[2]) - max(sq[0], b[0]))
            iy = max(0.0, min(sq[3], b[3]) - max(sq[1], b[1]))
            if area > 0 and ix * iy / area > 0.3:
                near.append(i)
        return near

    def known_place(self, img: Image.Image, box) -> bool:
        """Is this a confirmed fixture's place, with no confirmed animal seen here? (Where an animal
        has been confirmed, e.g. the lobster's crevice, every crop gets the full comparison.) Big crops
        never count: they're where something close to the lens shows up."""
        sq = square_box(img, box)
        if sq[2] - sq[0] >= SHORTCUT_MAX_PX:
            return False
        near = self._near(img, box)
        return any(not self.animal[i] for i in near) and not any(self.animal[i] for i in near)

    def similarity(self, img: Image.Image, box, emb):
        """(closest confirmed fixture, closest confirmed animal, that animal's name) at this crop's
        place; 0 and "" when there are none."""
        e = np.asarray(emb, np.float32) / np.linalg.norm(emb)
        fixture = animal = 0.0
        name = ""
        for i in self._near(img, box):
            sim = float(self.embs[i] @ e)
            if self.animal[i]:
                if sim > animal:
                    animal, name = sim, self.names[i]
            else:
                fixture = max(fixture, sim)
        return fixture, animal, name

    def save(self):
        try:
            self._save()
        except OSError as e:  # a helper: never let it stop a frame; it's saved again with the next change
            log.warning("fixture gallery not saved: %s", e)

    def _save(self):
        FIXTURE_GALLERY.parent.mkdir(parents=True, exist_ok=True)
        tmp = FIXTURE_GALLERY.with_name("fixture_gallery.tmp.npz")
        dim = len(self.embs[0]) if self.embs else 1024
        np.savez(tmp, model=self.model_name, boxes=np.array(self.boxes, np.float32).reshape(-1, 4),
                 embs=np.array(self.embs, np.float32).reshape(-1, dim), animal=np.array(self.animal, bool),
                 names=np.array(self.names, dtype=str),
                 sources=np.array(sorted(self.sources), dtype=str))
        os.replace(tmp, FIXTURE_GALLERY)


def is_structure(similarity, background_corr: float) -> bool:
    fixture, animal = similarity[:2]
    return fixture > animal and any(fixture >= g and background_corr >= c for g, c in FIXTURE_LOOKS)


class Tracker:
    def __init__(self):
        self._models = None
        self.last_model_use = datetime.now()
        DATA.mkdir(parents=True, exist_ok=True)
        self.hour = self._load_hour()
        self.last_prune = datetime.min
        self.last_crop = {}
        self.last_review = {"uncertain": {}, "check": {}}
        self.review_times = {"uncertain": deque(), "check": deque()}
        self.motion = Motion()
        self.background = Background()
        self._gallery = None
        self.fixtures_ignored = 0
        self.murky = False
        self.recent_visibility = deque(maxlen=3)
        self.db = db.connect()
        self.scene_first_seen = {}
        self.last_bank = datetime.min
        self.tracks = []
        self.scene_visits = {}  # name -> [first seen, last seen, looks, best confidence]
        self.last_regular = None
        self.bursts = deque()  # start times of recent bursts, for the hourly budget

    def process(self, img: Image.Image, when: datetime):
        self._end_visits(when)
        self._keep_recent(img, when)
        if self.last_regular and (when - self.last_regular).total_seconds() < REGULAR_SECONDS:
            self._burst_frame(img, when)
            return []
        self.last_regular = when
        self._roll_hour(when)
        self.hour["analyzed"] += 1
        stamp = when.isoformat(timespec="seconds")
        if is_dark(img):
            self.live = []
            self._write_live(img, when, "dark")
            self.recent_visibility.clear()  # a new day starts fresh
            self.hour["dark"] += 1
            self._save_hour()
            db.record_snapshot(self.db, stamp, True, [])
            return []

        # Water clarity: too murky and nothing is identified (like night); poor and only sure
        # things are named. Murky frames count as effort lost, not as "no animals".
        self.recent_visibility.append(visibility(img))
        clarity = float(np.median(self.recent_visibility))  # last 3 frames, so the state doesn't flicker
        if clarity < VISIBILITY_MIN:
            self.hour["murky"] = self.hour.get("murky", 0) + 1
            self._save_hour()
            db.record_snapshot(self.db, stamp, False, [], murky=True, visibility=clarity)
            if not self.murky:
                log.info("%s: water too murky to identify anything (visibility %.2f)", when.strftime("%H:%M:%S"), clarity)
            self.murky = True
            return []
        if self.murky:
            log.info("%s: water clear enough again (visibility %.2f)", when.strftime("%H:%M:%S"), clarity)
        self.murky = False
        poor = clarity < VISIBILITY_POOR

        started = time.perf_counter()
        if not self.motion.update(img, when):
            self._save_hour()
            db.record_snapshot(self.db, stamp, False, [], visibility=clarity)
            return []  # still learning what the empty scene looks like
        if not poor:
            self.background.add(img, when)
        self.record, self.live = None, []
        sightings = self._identify(img, when, poor)
        self._write_live(img, when, "poor visibility" if poor else "ok")
        if self.record and (self.record["ignored"] or self.record["named"]):
            (RECENT / f"{when:%H%M%S}.json").write_text(json.dumps(self.record), encoding="utf-8")
        for s in sightings:
            seen = self.hour["species"].setdefault(s.common, [0, 0])
            seen[0] += 1
            seen[1] = max(seen[1], s.count)
        self._save_hour()
        db.record_snapshot(self.db, stamp, False, sightings, visibility=clarity)
        self._bank(img, when, sightings)
        log.info("%s: %s (%.2fs%s%s)", when.strftime("%H:%M:%S"),
                 ", ".join(f"{s.count} {s.common}" for s in sightings) or "nothing",
                 time.perf_counter() - started, ", poor visibility" if poor else "",
                 f", {self.fixtures_ignored} fixed thing(s) ignored" if self.fixtures_ignored else "")
        return sightings

    def _identify(self, img: Image.Image, when: datetime, poor=False):
        """poor: the water is hazy. Only very sure names are logged (else the look-alike group or
        "unidentified"), and nothing goes to the review queue: a person couldn't judge it either."""
        m = self.models()
        species_cut = SPECIES_MIN_PROB_POOR if poor else SPECIES_MIN_PROB
        group_cut = GROUP_MIN_PROB_POOR if poor else GROUP_MIN_PROB
        scene_cut = SCENE_SURE_PROB if poor else SCENE_MIN_PROB
        queue = (lambda *a, **k: None) if poor else self._queue_review
        found = defaultdict(lambda: [0, 0.0, None, "zero-shot"])  # name -> [count, best prob, label, method]

        def add(label, prob, count=1, method="zero-shot"):
            entry = found[label["common"]]
            entry[0] += count
            entry[1] = max(entry[1], prob)
            entry[2] = label
            entry[3] = method

        # Fish boxes, keeping only those where something moved: the detector sometimes boxes the
        # pilings or the rope hanging from the pier, and those never move.
        fish_boxes = [b for b in m.detect(img)
                      if min(b[2] - b[0], b[3] - b[1]) >= MIN_CROP_PX and self.motion.fraction(b) >= BOX_MIN_MOTION]
        # ...and the growth hanging there, which does move (it sways) but is part of the background.
        corr = {id(b): self.background.correlation(img, b) for b in fish_boxes if max(b[2] - b[0], b[3] - b[1]) >= MIN_NAME_PX}
        fixtures = [b for b in fish_boxes if corr.get(id(b), -1) >= FIXTURE_SURE_CORR and self.gallery().known_place(img, b)]
        fish_boxes = [b for b in fish_boxes if b not in fixtures]
        self.record = record = {"ignored": [], "named": []}  # for frames/underwater/recent, with the scores
        self.live = live = []  # for the community viewer: what's in this frame

        def seen_here(box, name, prob, status):
            live.append({"box": [round(float(v)) for v in box[:4]], "name": name, "prob": round(float(prob), 3),
                         "status": status})

        def log_confirmed(view, box, emb):
            """Looks just like a picture a person confirmed as this animal: logged as it."""
            label, sim = self.confirmed_as
            name = label["common"]
            visit = self.scene_visits.setdefault(name, [when, when, 0, 0.0])
            visit[1], visit[2], visit[3] = when, visit[2] + 1, max(visit[3], sim)
            if name not in found:
                self._save_crop(view, when, name, sim)
                queue(view, img, box[:4], [{"common": name, "scientific": label["scientific"],
                                            "category": label["category"], "prob": round(sim, 3)}],
                      when, logged_as=name, embedding=emb)
                add(label, sim, method="confirmed look-alike")
            seen_here(box, name, sim, "logged")

        # 1. Everything that isn't a fish the detector found (octopus, crabs, lobsters, jellies, sea
        #    lions, rays, divers...): look at what moved. Each moving area bigger than a small fish,
        #    and the whole frame when a lot of it moved, is compared against every label. It only
        #    counts when a "scene" animal wins with at least SCENE_MIN_PROB; a close call goes to
        #    the review queue.
        full = (0, 0, img.width, img.height)
        views = [b for b in self.motion.blobs(BLOB_MIN_PX) if all(iou(b, f) < 0.3 for f in fish_boxes + fixtures)]
        views = views[:MAX_BLOBS]
        corr.update({id(b): self.background.correlation(img, b) for b in views})
        swaying = [b for b in views if corr[id(b)] >= FIXTURE_SURE_CORR and self.gallery().known_place(img, b)]
        views = [b for b in views if b not in swaying]
        sure = fixtures + swaying
        record["ignored"] += [{"box": [round(v) for v in b[:4]], "background": round(corr[id(b)], 3)} for b in sure]
        self.fixtures_ignored = len(sure)
        views += [full] if self.motion.moving_share() >= SCENE_MIN_MOTION else []
        whole_frame_animal = None
        for box in views:
            view = img if box == full else square_crop(img, box)
            emb = m.embed(view)
            verdict = "ok" if box == full else self._structure_check(img, box, emb, corr[id(box)], record)
            if verdict == "structure":
                continue
            if verdict == "confirmed":
                log_confirmed(view, box, emb)
                continue
            probs = m.probabilities(emb)
            if verdict == "suspect":
                if not m.labels[int(probs.argmax())]["negative"]:
                    queue(view, img, box, top_guesses(m.labels, probs), when, embedding=emb)
                    if box != full:
                        seen_here(box, m.labels[int(probs.argmax())]["common"], probs.max(), "unsure")
                continue
            label, prob = m.name(probs, None, scene_cut)
            if label is None:
                best = int(probs.argmax())
                top = m.labels[best]
                if top["scene"] and not top["negative"] and probs[best] >= REVIEW_SCENE_PROB:
                    queue(view, img, box, top_guesses(m.labels, probs), when, embedding=emb)
                    if box != full:
                        seen_here(box, top["common"], probs[best], "unsure")
                continue
            if not label["scene"]:
                continue  # a fish: the detector's job
            name = label["common"]
            if box != full and prob < SCENE_SURE_PROB:
                first = self.scene_first_seen.get(name)
                if not first or when - first > SCENE_REPEAT:
                    self.scene_first_seen[name] = when  # wait for it to show up again
                    queue(view, img, box, top_guesses(m.labels, probs), when, embedding=emb)
                    seen_here(box, name, prob, "unsure")
                    continue
            visit = self.scene_visits.setdefault(name, [when, when, 0, 0.0])
            visit[1], visit[2], visit[3] = when, visit[2] + 1, max(visit[3], prob)
            if name not in found:
                self._save_crop(view, when, name, prob)
                if not label.get("group"):
                    queue(view, img, box, top_guesses(m.labels, probs), when, logged_as=name,
                                       embedding=emb)
                add(label, prob)
            if box != full:
                seen_here(box, name, prob, "logged")
            if box == full:
                whole_frame_animal = label

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
            verdict = self._structure_check(img, box, emb, corr.get(id(box), -1), record)
            if verdict == "structure":
                continue
            if verdict == "confirmed":  # e.g. the lobster's antenna, which the fish detector boxed
                log_confirmed(crop, box, emb)
                continue
            probs = m.probabilities(emb, m.fish_idx)
            best = int(probs.argmax())
            if m.labels[m.fish_idx[best]]["negative"] and probs[best] >= NEGATIVE_MIN_PROB:
                continue  # murk, kelp or piling, not an animal
            if verdict == "suspect":
                guesses = top_guesses(m.labels, probs, m.fish_idx)
                if guesses and guesses[0]["prob"] >= REVIEW_FISH_PROB:
                    queue(crop, img, box[:4], guesses, when, embedding=emb, subset=m.fish_idx)
                    seen_here(box, guesses[0]["common"], guesses[0]["prob"], "unsure")
                continue
            camera = m.camera_name(emb)
            if camera and camera[0] == "not an animal":
                continue  # learned from people's answers: this kind of thing isn't an animal
            track = self._follow(box, probs, when)
            self._request_burst(when)
            if track.looks > 1:
                probs = track.probs_sum / track.looks  # the visit's average look: steadier than one frame
            label, prob = camera or m.name(probs, m.fish_idx, species_cut, group_cut)
            track.name, track.confidence = (label["common"], prob) if label else ("", prob)
            method = "camera-trained" if camera else "zero-shot"
            guesses = top_guesses(m.labels, probs, m.fish_idx)
            if label is None or label.get("group"):
                # Not sure of the species: a person may be able to tell.
                if guesses and guesses[0]["prob"] >= REVIEW_FISH_PROB:
                    queue(crop, img, box[:4], guesses, when, embedding=emb, subset=m.fish_idx)
                if label is None:
                    label, prob, method = UNIDENTIFIED, float(box[4]), "detector only"  # not even the group
            else:
                queue(crop, img, box[:4], guesses, when, logged_as=label["common"], embedding=emb, subset=m.fish_idx)
            add(label, prob, method=method)
            self._save_crop(crop, when, label["common"], prob)
            seen_here(box, label["common"], prob, "unsure" if label is UNIDENTIFIED else "logged")

        # 3. Small fish aren't tracked one by one: a school is one sighting a snapshot, with a rough
        #    size. The detector only boxes a few of them, so the size comes from counting the small
        #    moving specks, but only once the detector confirms there are fish at all.
        if small_scores:
            size = max(len(small_scores), self.motion.small_movers(MIN_NAME_PX))
            add(SMALL_FISH, max(small_scores), rough(size) if size >= SCHOOL_MIN else len(small_scores),
                method="detector only")

        # Five or more of one kind in a snapshot is logged as that kind's school.
        return [Sighting(l["common"] + (" (school)" if n >= SCHOOL_MIN else ""), l["scientific"], l["category"], n, p, how)
                for n, p, l, how in found.values()]

    # --- following fish across frames ---------------------------------------------------------
    def _follow(self, box, probs, when: datetime) -> Track:
        """The visit this fish belongs to: the closest recent track, or a new one."""
        near = [t for t in self.tracks if when - t.last_seen <= TRACK_GAP and t.near(box)]
        if near:
            track = min(near, key=lambda t: (t.box[0] - box[0]) ** 2 + (t.box[1] - box[1]) ** 2)
            track.box, track.last_seen = tuple(box[:4]), when
            track.probs_sum = track.probs_sum + probs
            track.looks += 1
            return track
        track = Track(when, when, tuple(box[:4]), probs.copy())
        self.tracks.append(track)
        return track

    def _request_burst(self, when: datetime):
        """Ask LiveCams for a frame every ~3 s for a while, within an hourly budget."""
        while self.bursts and when - self.bursts[0] > timedelta(hours=1):
            self.bursts.popleft()
        if len(self.bursts) * timedelta(seconds=BURST_SECONDS) >= BURST_BUDGET:
            return
        if self.bursts and when - self.bursts[-1] < timedelta(seconds=BURST_SECONDS):
            return  # the current burst is still running
        self.bursts.append(when)
        try:
            BURST_FLAG.write_text(str(int(time.time()) + BURST_SECONDS), encoding="utf-8")
        except OSError:
            pass

    def _burst_frame(self, img: Image.Image, when: datetime):
        """A frame between the regular ones: only used to add looks to the fish being followed."""
        if not self.tracks or is_dark(img) or visibility(img) < VISIBILITY_POOR:
            return
        m = self.models()
        self.motion.peek(img)
        for box in m.detect(img):
            if max(box[2] - box[0], box[3] - box[1]) < MIN_NAME_PX or self.motion.fraction(box) < BOX_MIN_MOTION:
                continue
            c = self.background.correlation(img, box)
            if c >= FIXTURE_SURE_CORR and self.gallery().known_place(img, box):
                continue
            emb = m.embed(square_crop(img, box))
            if self._structure_check(img, box, emb, c, {"ignored": [], "named": []}) != "ok":
                continue
            probs = m.probabilities(emb, m.fish_idx)
            if any(t.near(box) and when - t.last_seen <= TRACK_GAP for t in self.tracks):
                self._follow(box, probs, when)

    def _end_visits(self, when: datetime):
        """Records the animals that have left: one row per visit. Fish are followed by position and named
        from all their looks; other animals (a lobster on a piling, a turtle, a seal) by name."""
        for name, (first, last, looks, conf) in list(self.scene_visits.items()):
            if when - last > SCENE_VISIT_GAP:
                db.record_visit(self.db, first.isoformat(timespec="seconds"), last.isoformat(timespec="seconds"),
                                looks, name, conf)
                del self.scene_visits[name]
        done = [t for t in self.tracks if when - t.last_seen > TRACK_GAP]
        self.tracks = [t for t in self.tracks if t not in done]
        if not done or self._models is None:
            return
        m = self._models
        for t in done:
            avg = t.probs_sum / t.looks
            label, prob = m.name(avg, m.fish_idx, SPECIES_MIN_PROB, GROUP_MIN_PROB)
            db.record_visit(self.db, t.first_seen.isoformat(timespec="seconds"), t.last_seen.isoformat(timespec="seconds"),
                            t.looks, label["common"] if label else UNIDENTIFIED["common"], prob)

    def _queue_review(self, view: Image.Image, frame: Image.Image, box, guesses, when: datetime, logged_as=None,
                      embedding=None, subset=None):
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
            "embedding_model": self._models.embedding_model if self._models else "",
            # shadow mode: the reference-photo classifier's call, compared later with the person's answer
            "shadow": (self._models.reference_opinion(np.asarray(embedding), subset)
                       if self._models is not None and embedding is not None else None),
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

    def gallery(self) -> "FixtureGallery":
        if self._gallery is None:
            self._gallery = FixtureGallery(self.models().embedding_model)
        return self._gallery

    def _structure_check(self, img, box, emb, background_corr, record) -> str:
        """"structure": looks like a confirmed fixture here, ignore it. "suspect": as alike to the
        background as structure, but nobody has confirmed what's here, so a person decides (not
        logged). "ok": log it."""
        like_fixture, like_animal, animal_name = self.gallery().similarity(img, box, emb)
        label = self._models.label_named(animal_name) if animal_name else None
        if (label and like_animal >= ANIMAL_NAME_SIM and like_animal > like_fixture
                and label["category"] not in ("fish", "shark/ray")):
            verdict = "confirmed"
            self.confirmed_as = (label, like_animal)
        elif is_structure((like_fixture, like_animal), background_corr):
            verdict = "structure"
        elif background_corr >= SUSPECT_CORR and not (like_animal >= ANIMAL_LOOKS and like_animal > like_fixture):
            verdict = "suspect"
        else:
            verdict = "ok"
        record["ignored" if verdict == "structure" else "named"].append(
            {"box": [round(v) for v in box[:4]], "background": round(background_corr, 3),
             "like_fixture": round(like_fixture, 3), "like_animal": round(like_animal, 3), "verdict": verdict,
             **({"as": animal_name} if verdict == "confirmed" else {})})
        if verdict == "structure":
            self.fixtures_ignored += 1
        return verdict

    def _write_live(self, img, when: datetime, status: str):
        """frames/underwater/live.json: this frame's detections, for the community viewer."""
        try:
            tmp = LIVE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"taken_at": when.isoformat(timespec="seconds"), "status": status,
                                       "width": img.width, "height": img.height,
                                       "detections": getattr(self, "live", [])}), encoding="utf-8")
            os.replace(tmp, LIVE)
        except OSError:
            pass  # a viewer feature: never stops tracking

    @staticmethod
    def _keep_recent(img: Image.Image, when: datetime):
        """Keeps this frame for 10 minutes (daylight only), dropping older ones."""
        if is_dark(img):
            return
        RECENT.mkdir(parents=True, exist_ok=True)
        img.save(RECENT / f"{when:%H%M%S}.jpg", quality=80)
        cutoff = (when - KEEP_RECENT).strftime("%H%M%S")
        for old in RECENT.iterdir():
            # names are times of day; after midnight everything from yesterday evening is older anyway
            if old.stem < cutoff or old.stem > when.strftime("%H%M%S"):
                old.unlink(missing_ok=True)

    def _bank(self, img: Image.Image, when: datetime, sightings):
        named = [s.common for s in sightings if s.common.removesuffix(" (school)") not in ("small fish", "fish (unidentified)")]
        if when - self.last_bank < (BANK_INTERESTING_EVERY if named else BANK_EVERY):
            return
        self.last_bank = when
        folder = BANK / when.date().isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        tag = "_" + file_safe(named[0]) if named else ""
        img.save(folder / f"{when:%H%M%S}{tag}.jpg", quality=92)

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
        return {"date": when.date().isoformat(), "hour": when.hour, "analyzed": 0, "dark": 0, "murky": 0, "species": {}}

    def _roll_hour(self, when: datetime):
        h = self.hour
        if (h["date"], h["hour"]) == (when.date().isoformat(), when.hour):
            return
        self.hour = self._new_hour(when)
        if self._gallery is not None:
            self._gallery.add_reviewed()  # new answers from the review window
        if when - self.last_prune > timedelta(hours=6):
            self.last_prune = when
            self._prune_crops(when)

    def _save_hour(self):
        # Written to a temporary file, then swapped in: an interruption leaves the old one intact.
        tmp = HOUR_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.hour), encoding="utf-8")
        os.replace(tmp, HOUR_STATE)

    def close(self):
        self._save_hour()
        if self._gallery is not None:
            self._gallery.save()
        self.db.close()

    @staticmethod
    def _prune_crops(now: datetime):
        cutoff = (now - timedelta(days=KEEP_CROPS_DAYS)).date().isoformat()
        for folder in CROPS.glob("*"):
            if folder.is_dir() and folder.name < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        bank_cutoff = (now - timedelta(days=KEEP_BANK_DAYS)).date().isoformat()
        for folder in BANK.glob("*"):
            if folder.is_dir() and folder.name < bank_cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        stale = (now - timedelta(days=KEEP_PENDING_DAYS)).strftime("%Y%m%d")
        for f in REVIEW.glob("*"):
            if f.name[:8] < stale:
                f.unlink(missing_ok=True)

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
    tracker = Tracker()
    last_mtime = None
    STOP_FILE.unlink(missing_ok=True)  # left over from a crash
    while not STOP_FILE.exists():
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
    tracker.close()
    log.info("stopped (asked by LiveCams)")


if __name__ == "__main__":
    main()
