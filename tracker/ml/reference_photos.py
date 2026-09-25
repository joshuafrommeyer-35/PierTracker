"""Reference photos: teach the tracker what each species looks like through this camera's water.

The tracker names animals by matching a picture against species *names* (BioCLIP zero-shot). This adds a
small classifier trained on research-grade iNaturalist photos of the same species, degraded to look like
this camera (green-blue water, murk, blur, small size, JPEG), plus crops of this camera's own background
as "not an animal". On held-out photos (2026-09-25) it named more fish correctly with fewer confident
mistakes than names alone (the report has the numbers).

It runs in **shadow mode**: its opinion is stored on every review picture next to the tracker's, and
each night `evaluate` compares both with the answers people gave. It's only worth switching on once it
beats the names on those real answers (MIN_ANSWERS, MIN_GAIN); the report says when.

    tracker\\.venv\\Scripts\\python tracker\\ml\\reference_photos.py fetch     # photos, ~46 per species (~1 h)
    tracker\\.venv\\Scripts\\python tracker\\ml\\reference_photos.py embed     # through the tracker's model (~2 h, iGPU)
    tracker\\.venv\\Scripts\\python tracker\\ml\\reference_photos.py train     # fit + test -> models/reference_probe.npz
    tracker\\.venv\\Scripts\\python tracker\\ml\\reference_photos.py evaluate  # vs. people's answers (run nightly)

Photos and embeddings stay in data/reference_photos (local; iNaturalist photos keep their own licenses).
"""

import csv
import io
import json
import sys
import time
import urllib.parse
import urllib.request
import zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

TRACKER = Path(__file__).resolve().parent.parent
LIVECAMS = TRACKER.parent
sys.path.insert(0, str(TRACKER))
REF = LIVECAMS / "data" / "reference_photos"
PHOTOS, TEST_PHOTOS, JUVENILE, EMB = REF / "photos", REF / "test_photos", REF / "juvenile_blacksmith", REF / "embeddings"
SPECIES = TRACKER / "species.json"
OUT = TRACKER / "models" / "reference_probe.npz"
REPORT = LIVECAMS / "data" / "ml" / "reference_probe_report.md"
PUBLIC = LIVECAMS / "results" / "reference_probe.md"
REVIEW = LIVECAMS / "data" / "review"
PER_SPECIES = 46
C, W = 10.0, 0.75            # regularization and mix weight, chosen on held-out reference photos
UNDERWATER_MIN = 0.5         # reference photos must look like underwater photos (no fish on a boat deck)
MIN_ANSWERS, MIN_GAIN = 30, 0.05
WATER = [np.array([8, 148, 115]) / 255, np.array([14, 137, 158]) / 255]  # measured on this camera
UNDERWATER_TEXT = ["an underwater photo of a fish swimming.", "an underwater photo taken while scuba diving or snorkeling.",
                   "an underwater photo of a marine animal on a reef, in kelp or on the sea floor.",
                   "an underwater photo of a sea turtle, seal or jellyfish in the ocean."]
DRY_TEXT = ["a photo of a fish caught by an angler, held in a hand.", "a photo of a dead fish lying on a boat deck, a dock or the ground.",
            "a photo of a fish on a table, in a bucket, a cooler or at a fish market.",
            "a photo of an animal on a beach, on rocks or on a pier, out of the water.",
            "a photo of a bird standing on a rock or flying.", "a photo of an animal washed up on the sand."]


def species_list():
    return json.loads(SPECIES.read_text(encoding="utf-8"))["species"]


def folder_name(common):
    return common.replace(" ", "_")


def is_test(photo_id: str) -> bool:
    """About a fifth of the reference photos are held out for testing, by observation id."""
    return int(photo_id) % 23 < 5


# ---------- fetch ----------
def fetch():
    ua = {"User-Agent": "PierTracker/1.0 (reference photos for a camera classifier)"}

    def get(url):
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=60) as r:
                    return r.read()
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(5)

    taken = {p.stem for p in TEST_PHOTOS.rglob("*.jpg")} | {p.stem for p in JUVENILE.glob("*.jpg")}
    for sp in species_list():
        if not sp["scientific"]:
            continue
        folder = PHOTOS / folder_name(sp["common"])
        folder.mkdir(parents=True, exist_ok=True)
        have = {p.stem for p in folder.glob("*.jpg")}
        for place in ({"place_id": 14}, {}):  # California first, then anywhere
            for page in (1, 2):
                if len(have) >= PER_SPECIES:
                    break
                q = urllib.parse.urlencode({"taxon_name": sp["scientific"], "quality_grade": "research", "photos": "true",
                                            "per_page": 100, "page": page, "order_by": "votes", **place})
                results = json.loads(get(f"https://api.inaturalist.org/v1/observations?{q}"))["results"]
                time.sleep(1.0)  # iNaturalist asks for at most ~1 request a second
                for o in results:
                    oid = str(o["id"])
                    if len(have) >= PER_SPECIES or oid in have or oid in taken or not o.get("photos"):
                        continue
                    try:
                        (folder / f"{oid}.jpg").write_bytes(get(o["photos"][0]["url"].replace("square", "medium")))
                        have.add(oid)
                    except OSError:
                        pass
                    time.sleep(0.25)
                if len(results) < 100:
                    break
        print(sp["common"], len(have), flush=True)


# ---------- embed ----------
def jpeg(a, quality):
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(np.clip(a * 255, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=quality)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def degrade_test(crop, rng):
    """The camera simulation calibrated on this camera (used in every test)."""
    from PIL import Image
    size = int(rng.choice([80, 100, 130]) * 1.3)
    a = np.asarray(crop.resize((size, size), Image.BICUBIC), np.float32) / 255
    t = np.array([0.12, 0.8, 0.75]) + rng.uniform(-0.05, 0.05, 3)
    return jpeg(a * t + WATER[int(rng.integers(2))] * (1 - t) + rng.normal(0, 0.015, a.shape), 70)


def degrade_ref(crop, rng):
    """Broader than the test simulation, so a gain can't come from matching it exactly."""
    from PIL import Image, ImageFilter
    img = crop.resize((int(rng.uniform(70, 200) * 1.3),) * 2, Image.BICUBIC)
    if rng.random() < 0.4:
        img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.5, 1.8)))
    a = np.asarray(img, np.float32) / 255
    mix = rng.random()
    water = np.clip(WATER[0] * mix + WATER[1] * (1 - mix) + rng.normal(0, 0.03, 3), 0, 1)
    t = np.clip(np.array([0.12, 0.8, 0.75]) * rng.uniform(0.6, 1.25) + rng.uniform(-0.08, 0.08, 3), 0.03, 1)
    return jpeg(a * t + water * (1 - t) + rng.normal(0, rng.uniform(0.005, 0.03), a.shape), int(rng.integers(55, 86)))


def filter_text():
    path = EMB / "_filter_text.npz"
    if not path.exists():
        import open_clip
        import torch
        import torch.nn.functional as F
        model_name = json.loads((TRACKER / "models" / "classifier.json").read_text(encoding="utf-8"))["model"]
        model, _, _ = open_clip.create_model_and_transforms(model_name)
        tok = open_clip.get_tokenizer(model_name)
        with torch.no_grad():
            emb = F.normalize(model.eval().encode_text(tok(UNDERWATER_TEXT + DRY_TEXT)), dim=-1).numpy()
        np.savez(path, emb=emb.astype(np.float32), underwater=np.array([True] * len(UNDERWATER_TEXT) + [False] * len(DRY_TEXT)))
    d = np.load(path)
    return d["emb"], d["underwater"]


def embed():
    """Per species: the whole photo (for the underwater check) and 2 degraded crops of the main animal.
    Plus random squares of this camera's frame bank that don't touch a detected fish (background)."""
    import tracker as T
    from PIL import Image
    m = T.Models()
    EMB.mkdir(parents=True, exist_ok=True)
    filter_text()
    for sp in species_list():
        if not sp["scientific"]:
            continue
        dest = EMB / f"{folder_name(sp['common'])}.npz"
        if dest.exists():
            continue
        rng = np.random.default_rng(zlib.crc32(sp["common"].encode()))
        photos = [(f, "test" if is_test(f.stem) else "ref") for f in sorted((PHOTOS / folder_name(sp["common"])).glob("*.jpg"))]
        photos += [(f, "test") for f in sorted((TEST_PHOTOS / folder_name(sp["common"])).glob("*.jpg"))]
        if sp["common"] == "blacksmith":
            photos += [(f, "test_juvenile") for f in sorted(JUVENILE.glob("*.jpg"))]
        full, deg, deg_photo, split, ids = [], [], [], [], []
        for f, s in photos:
            try:
                img = Image.open(f).convert("RGB")
            except OSError:
                continue
            k = len(ids)
            ids.append(f.stem)
            split.append(s)
            full.append(m.embed(img))
            boxes = m.detect(img)
            if boxes:
                crop = T.square_crop(img, max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])))
            else:
                side = int(min(img.size) * 0.9)
                crop = img.crop(((img.width - side) // 2, (img.height - side) // 2, (img.width + side) // 2, (img.height + side) // 2))
            for _ in range(2):
                deg.append(m.embed(degrade_ref(crop, rng) if s == "ref" else degrade_test(crop, rng)))
                deg_photo.append(k)
        norm = lambda a: np.array(a, np.float32) / np.linalg.norm(np.array(a, np.float32), axis=1, keepdims=True)  # noqa: E731
        np.savez(dest, full=norm(full), deg=norm(deg), deg_photo=np.array(deg_photo), split=np.array(split), ids=np.array(ids))
        print(sp["common"], len(ids), "photos", flush=True)
    bg = EMB / "_background.npz"
    if not bg.exists():
        rng = np.random.default_rng(0)
        embs = []
        for f in sorted((LIVECAMS / "data" / "frame_bank").rglob("*.jpg")):
            img = Image.open(f).convert("RGB")
            boxes = m.detect(img)
            n = 0
            for _ in range(400):
                if n >= 40:
                    break
                side = int(rng.uniform(80, 260))
                x, y = int(rng.integers(0, img.width - side)), int(rng.integers(0, img.height - side))
                box = (x, y, x + side, y + side)
                if any(b[0] < box[2] and box[0] < b[2] and b[1] < box[3] and box[1] < b[3] for b in boxes):
                    continue
                e = m.embed(img.crop(box))
                embs.append(e / np.linalg.norm(e))
                n += 1
        np.savez(bg, emb=np.array(embs, np.float32))
        print("background crops", len(embs), flush=True)


# ---------- train ----------
def load():
    """Reference crops (underwater photos only), test crops, background crops."""
    ft, uw_mask = filter_text()
    meta = json.loads((TRACKER / "models" / "classifier.json").read_text(encoding="utf-8"))
    scale = meta["logit_scale"]
    names = {s["common"] for s in species_list()}
    ref_X, ref_y, test = [], [], []
    for f in sorted(EMB.glob("*.npz")):
        common = f.stem.replace("_", " ")
        if f.name.startswith("_") or common not in names:
            continue
        d = np.load(f)
        z = scale * d["full"] @ ft.T
        p = np.exp(z - z.max(1, keepdims=True))
        p /= p.sum(1, keepdims=True)
        underwater = p[:, uw_mask].sum(1) >= UNDERWATER_MIN
        for e, k in zip(d["deg"], d["deg_photo"]):
            if d["split"][k] == "ref" and underwater[k]:
                ref_X.append(e)
                ref_y.append(common)
            elif d["split"][k] != "ref":
                test.append((e, common, bool(underwater[k]), d["split"][k] == "test_juvenile"))
    bg = np.load(EMB / "_background.npz")["emb"]
    return np.array(ref_X), np.array(ref_y), test, bg


def fit(ref_X, ref_y, bg):
    from sklearn.linear_model import LogisticRegression
    X = np.vstack([ref_X, bg])
    y = np.concatenate([ref_y, np.full(len(bg), "not an animal")])
    clf = LogisticRegression(C=C, max_iter=3000).fit(X, y)
    return {"coef": clf.coef_.astype(np.float32), "intercept": clf.intercept_.astype(np.float32),
            "classes": np.array(clf.classes_, dtype=str), "w": np.float32(W)}


def train():
    import tracker as T
    meta = json.loads((TRACKER / "models" / "classifier.json").read_text(encoding="utf-8"))
    labels, scale = meta["labels"], meta["logit_scale"]
    E = np.load(TRACKER / "models" / "label_embeddings.npy")
    E = E / np.linalg.norm(E, axis=1, keepdims=True)
    fish = np.array([i for i, l in enumerate(labels) if l["negative"] or l["category"] in ("fish", "shark/ray")])
    names = [labels[i]["common"] for i in fish]
    negative = np.array([labels[i]["negative"] for i in fish])
    group_of = {s["common"]: s.get("group") for s in species_list()}

    ref_X, ref_y, test, bg = load()
    half = len(bg) // 2  # background crops are stored frame by frame: train on the first frames, test on the rest
    probe = fit(ref_X, ref_y, bg[:half])

    def zero_shot(e):
        z = scale * E[fish] @ e
        p = np.exp(z - z.max())
        return p / p.sum()

    def with_probe(e):
        return T.combine_reference(zero_shot(e), names, negative, T.reference_probs(probe, e),
                                   [str(c) for c in probe["classes"]], W)

    def call(p, cut):
        """The tracker's rule: a species at `cut`, else its look-alike group at max(cut + 0.05, 0.90)."""
        animal = np.where(~negative)[0]
        b = animal[int(p[animal].argmax())]
        if p[b] >= cut:
            return names[b]
        totals = defaultdict(float)
        for i in animal:
            if group_of.get(names[i]):
                totals[group_of[names[i]]] += p[i]
        if totals:
            g, v = max(totals.items(), key=lambda kv: kv[1])
            if v >= max(cut + 0.05, 0.90):
                return g
        return None

    fish_test = [(e, c, juv) for e, c, uw, juv in test if uw and c in names]
    bg_test = bg[half:]
    cache = {}  # method -> (probabilities for the test crops, for the background crops), computed once

    def score(method, cut):
        if method not in cache:
            cache[method] = ([method(e) for e, _, _ in fish_test], [method(e) for e in bg_test])
        test_p, bg_p = cache[method]
        calls = [(call(p, cut), c) for p, (_, c, juv) in zip(test_p, fish_test) if not juv]
        named = [(n, c) for n, c in calls if n]
        right = sum(n == c or n == group_of.get(c) for n, c in named)
        alarms = np.mean([call(p, cut) is not None for p in bg_p])
        juv = [call(p, cut) for p, (_, c, j) in zip(test_p, fish_test) if j]
        return {"cut": cut, "named": len(named) / max(len(calls), 1), "right_of_named": right / max(len(named), 1),
                "right_of_all": right / max(len(calls), 1), "false_alarms": float(alarms),
                "juvenile_right": sum(n == "blacksmith" for n in juv), "juvenile_wrong": sum(n not in (None, "blacksmith") for n in juv),
                "juvenile_n": len(juv)}

    now = score(zero_shot, 0.85)
    matched = next((score(with_probe, c) for c in np.round(np.arange(0.5, 0.99, 0.01), 2)
                    if score(with_probe, c)["false_alarms"] <= now["false_alarms"] + 1e-9), score(with_probe, 0.99))

    probe_all = fit(ref_X, ref_y, bg)  # the saved one uses all the background crops
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez(OUT, **probe_all, embedding_model=meta["model"], trained_at=datetime.now().isoformat(timespec="seconds"))
    species_known = sorted(set(ref_y))
    lines = [
        f"# Reference-photo classifier ({datetime.now():%Y-%m-%d})", "",
        f"Trained on {len(ref_X)} degraded crops from underwater reference photos of {len(species_known)} species, plus "
        f"{len(bg)} crops of this camera's background as \"not an animal\". Species with no underwater reference photo "
        "are left to zero-shot.", "",
        f"**Test** on {len(fish_test)} crops of held-out underwater photos (simulated camera conditions), and on "
        f"{len(bg_test)} background crops from frames the classifier didn't train on. The classifier's cutoff is set so "
        "its false alarms on the background match the tracker's.", "",
        "| | Names only (now) | With the reference classifier |", "|---|---:|---:|",
        f"| Cutoff | {now['cut']:.2f} | {matched['cut']:.2f} |",
        f"| False alarms on this camera's background | {now['false_alarms']:.1%} | {matched['false_alarms']:.1%} |",
        f"| Fish named | {now['named']:.1%} | {matched['named']:.1%} |",
        f"| Right, of those named | {now['right_of_named']:.1%} | {matched['right_of_named']:.1%} |",
        f"| Right, of all | {now['right_of_all']:.1%} | {matched['right_of_all']:.1%} |",
        f"| Young blacksmith: right / confidently wrong (of {now['juvenile_n']}) | {now['juvenile_right']} / {now['juvenile_wrong']} "
        f"| {matched['juvenile_right']} / {matched['juvenile_wrong']} |", "",
        "Runs in **shadow mode**: see `evaluate` below for how it does on real review answers.", "",
        "Species it knows: " + ", ".join(species_known), "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ---------- evaluate on real answers ----------
def evaluate():
    """Compares the tracker's call and the shadow call with people's answers (reviewer = person) on the
    review pictures that carry a shadow opinion. Writes the result to the report and results/."""
    group_of = {s["common"]: s.get("group") for s in species_list()}

    def match(call, answer):
        return call == answer or (call and group_of.get(call) == answer)

    rows = []
    decisions = REVIEW / "decisions.csv"
    if decisions.exists():
        with decisions.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if (r.get("reviewer") or "person") != "person":
                    continue
                card = REVIEW / r["decision"] / Path(r["image"]).with_suffix(".json").name
                try:
                    d = json.loads(card.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                shadow = d.get("shadow")
                if not shadow or not d.get("guesses"):
                    continue
                answer = r["common_name"] if r["decision"] == "approved" else "not an animal"
                tracker_call = d["guesses"][0]["common"]  # it queued the picture, so it thought "animal"
                shadow_call = "not an animal" if shadow["not_animal"] >= 0.5 else shadow["name"]
                rows.append((answer, tracker_call, shadow_call))
    n = len(rows)
    tracker_right = sum(match(t, a) for a, t, _ in rows)
    shadow_right = sum(match(s, a) for a, _, s in rows)
    animals = [(a, t, s) for a, t, s in rows if a != "not an animal"]
    if n < MIN_ANSWERS:
        verdict = f"Not enough answers yet: {n} of the {MIN_ANSWERS} needed."
    elif (shadow_right - tracker_right) / n >= MIN_GAIN:
        verdict = "**Ready to switch on**: it beats the tracker's names on people's answers."
    else:
        verdict = "Not better than the tracker's names on people's answers (yet): stays in shadow mode."
    section = [
        f"## On people's review answers ({datetime.now():%Y-%m-%d})", "",
        f"{n} answered pictures with a shadow opinion. {verdict}", "",
    ]
    if n:
        section += ["| | Tracker (names only) | Reference classifier |", "|---|---:|---:|",
                    f"| Right | {tracker_right}/{n} ({tracker_right / n:.0%}) | {shadow_right}/{n} ({shadow_right / n:.0%}) |"]
        if animals:
            ta = sum(match(t, a) for a, t, _ in animals)
            sa = sum(match(s, a) for a, _, s in animals)
            section.append(f"| Right, when it was an animal ({len(animals)}) | {ta} ({ta / len(animals):.0%}) | {sa} ({sa / len(animals):.0%}) |")
        section.append("")
    base = REPORT.read_text(encoding="utf-8").split("## On people's review answers")[0] if REPORT.exists() else ""
    text = base.rstrip() + "\n\n" + "\n".join(section) + "\n"
    REPORT.write_text(text, encoding="utf-8")
    PUBLIC.parent.mkdir(exist_ok=True)
    PUBLIC.write_text(text, encoding="utf-8")
    print("\n".join(section))


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "evaluate"
    {"fetch": fetch, "embed": embed, "train": train, "evaluate": evaluate}[step]()
