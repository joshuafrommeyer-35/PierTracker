"""Trains a classifier fitted to this camera from the answers given in the review window.

Every review picture stores BioCLIP's image embedding (1,024 numbers describing the image), and a
person's answer is its label. A logistic regression on those embeddings learns what each animal
looks like *on this camera* (green-blue water, blur, backlight), which zero-shot matching against
species names can't. This script:

  1. collects the answered pictures (data/review/approved and rejected; answers from
     data/review/decisions.csv; "not an animal" answers are a class too),
  2. counts examples per animal (`--status` stops here),
  3. cross-validates the classifier and compares it with BioCLIP's zero-shot answer on the same
     pictures,
  4. if it clearly wins, saves tracker/models/camera_classifier.npz. The tracker picks it up on
     its own and uses it for the animals it was trained on (zero-shot still covers the rest).

It writes data/ml/classifier_report.md either way. Run by nightly.py once a day, or by hand:
    tracker\\.venv\\Scripts\\python tracker\\ml\\train_classifier.py [--status]
"""

import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

TRACKER = Path(__file__).resolve().parent.parent
LIVECAMS = TRACKER.parent
REVIEW = LIVECAMS / "data" / "review"
DECISIONS = REVIEW / "decisions.csv"
OUT = TRACKER / "models" / "camera_classifier.npz"
REPORT = LIVECAMS / "data" / "ml" / "classifier_report.md"
NOT_AN_ANIMAL = "not an animal"
MIN_PER_CLASS = 12     # an animal needs this many answers to be learned
MIN_CLASSES = 3        # ...and at least this many animals (incl. "not an animal") to train at all
MIN_GAIN = 0.05        # must beat zero-shot by 5 points of accuracy to be switched on
TARGET_PER_CLASS = 30  # what "enough" looks like, for the progress report


def current_embedding_model():
    meta = json.loads((TRACKER / "models" / "classifier.json").read_text(encoding="utf-8"))
    return meta.get("model", "hf-hub:imageomics/bioclip-2")


def examples():
    """(embeddings, labels, zero-shot guesses) for every answered picture with a stored embedding
    from the current model."""
    model = current_embedding_model()
    X, y, zero_shot = [], [], []
    if not DECISIONS.exists():
        return np.zeros((0, 0)), [], []
    with DECISIONS.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            folder = REVIEW / ("approved" if row["decision"] == "approved" else "rejected")
            item = folder / Path(row["image"]).with_suffix(".json").name
            if not item.exists():
                continue
            data = json.loads(item.read_text(encoding="utf-8"))
            if not data.get("embedding") or data.get("embedding_model", model) != model:
                continue
            X.append(data["embedding"])
            y.append(row["common_name"] if row["decision"] == "approved" else NOT_AN_ANIMAL)
            zero_shot.append(row["best_guess"])
    return np.array(X, np.float32), y, zero_shot


def status_lines(y):
    counts = Counter(y)
    lines = [f"{len(y)} answered pictures with embeddings. An animal is learned once it has "
             f"{MIN_PER_CLASS} answers; ~{TARGET_PER_CLASS} makes it reliable.", "",
             "| Animal | Answers | Progress |", "|---|---:|---|"]
    for name, n in counts.most_common():
        bar = "#" * min(n * 10 // TARGET_PER_CLASS, 10)
        lines.append(f"| {name} | {n} | `{bar:<10s}` {'ready' if n >= MIN_PER_CLASS else ''} |")
    return lines


def train(X, y, zero_shot):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import classification_report
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    counts = Counter(y)
    keep = [i for i, label in enumerate(y) if counts[label] >= MIN_PER_CLASS]
    classes = sorted({y[i] for i in keep})
    if len(classes) < MIN_CLASSES:
        return None, [f"Not trained yet: {len(classes)} of the {MIN_CLASSES} animals needed have "
                      f"{MIN_PER_CLASS}+ answers."]
    Xk, yk, zk = X[keep], [y[i] for i in keep], [zero_shot[i] for i in keep]
    folds = min(5, min(Counter(yk).values()))
    clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced")
    predicted = cross_val_predict(clf, Xk, yk, cv=StratifiedKFold(folds, shuffle=True, random_state=0))
    acc = float(np.mean([p == t for p, t in zip(predicted, yk)]))
    zs_acc = float(np.mean([z == t for z, t in zip(zk, yk)]))
    clf.fit(Xk, yk)
    enabled = acc >= zs_acc + MIN_GAIN
    lines = [f"Trained on {len(yk)} answers, {len(classes)} animals, {folds}-fold cross-validation:", "",
             f"- camera-trained classifier: **{100 * acc:.0f}%** right",
             f"- zero-shot BioCLIP on the same pictures: **{100 * zs_acc:.0f}%** right",
             f"- {'**switched on**: the tracker now uses it for these animals.' if enabled else 'kept off: it has to beat zero-shot by 5 points first.'}",
             "", "```", classification_report(yk, predicted, zero_division=0), "```"]
    model = {"coef": clf.coef_.astype(np.float32), "intercept": clf.intercept_.astype(np.float32),
             "classes": np.array(clf.classes_), "enabled": enabled, "cv_accuracy": acc,
             "zero_shot_accuracy": zs_acc, "embedding_model": current_embedding_model(),
             "trained_at": datetime.now().isoformat(timespec="seconds")}
    return model, lines


def main(status_only=False):
    X, y, zero_shot = examples()
    lines = [f"# Camera classifier ({datetime.now():%Y-%m-%d %H:%M})", ""] + status_lines(y) + [""]
    if not status_only:
        model, report = train(X, y, zero_shot) if len(y) else (None, ["No answers yet."])
        lines += report
        if model is not None:
            OUT.parent.mkdir(parents=True, exist_ok=True)
            np.savez(OUT, **model)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(status_only="--status" in sys.argv)
