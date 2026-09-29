"""Frames every 2 s: what passes between two snapshots is caught without inflating the statistics,
and empty water and dusk grain aren't named "octopus". Plus the review queue making room for rare animals."""
import json
from collections import deque
from datetime import datetime, timedelta

import numpy as np
from PIL import Image, ImageDraw

import tracker as T


def scene(rgb, noise=0.0):
    """Water of one colour with two dark pilings, optionally grainy."""
    img = np.zeros((1080, 1920, 3), np.float32) + rgb
    img[:, 300:500] *= 0.35
    img[:, 1300:1450] *= 0.35
    if noise:
        img += np.random.default_rng(0).normal(0, noise, img.shape)
    return Image.fromarray(img.clip(0, 255).astype(np.uint8))


def test_dusk_grain_counts_as_dark():
    assert not T.is_dark(scene((20, 110, 140)))       # daylight: blue-green
    # The same scene gone grey and grainy, bright enough and with the green channel leading, so only
    # its missing colour gives it away (the model called this "octopus" at 99%).
    assert T.is_dark(scene((70, 76, 72), noise=20))


def test_open_water_has_no_outline():
    water = Image.new("RGB", (120, 120), (30, 120, 150))
    gradient = Image.fromarray(np.tile(np.linspace(90, 150, 120)[:, None, None], (1, 120, 3)).astype(np.uint8))
    grainy = Image.fromarray((np.asarray(water, np.float32)
                              + np.random.default_rng(1).normal(0, 3, (120, 120, 3))).clip(0, 255).astype(np.uint8))
    fish = water.copy()
    ImageDraw.Draw(fish).ellipse((25, 45, 95, 75), fill=(10, 40, 50))
    for flat in (water, gradient, grainy):
        assert T.structure(flat) < T.FLAT_MAX
    assert T.structure(fish) > T.FLAT_MAX


def test_between_frames_stay_within_their_budget():
    t = T.Tracker.__new__(T.Tracker)  # just the budget, without models or a database
    now = datetime(2026, 9, 28, 12, 0)
    t.between_time = deque([(now - timedelta(minutes=70), 600.0), (now - timedelta(minutes=30), 800.0)])
    assert t._between_budget_left(now)   # the 70-minute-old look has dropped out: 800 of 900 s used
    t.between_time.append((now, 150.0))
    assert not t._between_budget_left(now)


def test_a_full_review_queue_makes_room_for_rare_animals(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "REVIEW_MAX_PENDING", 8)
    for i, name in enumerate(["blacksmith"] * 7 + ["salema"]):
        for ext in (".json", ".jpg"):
            (tmp_path / f"20260928-10{i:02d}00_{name}{ext}").write_text("{}")
    assert T.make_room_for_review("octopus", tmp_path)       # rare: the oldest blacksmith makes room
    assert not (tmp_path / "20260928-100000_blacksmith.json").exists()
    assert not (tmp_path / "20260928-100000_blacksmith.jpg").exists()
    (tmp_path / "20260928-110000_octopus.json").write_text("{}")  # the caller adds it: full again
    assert not T.make_room_for_review("blacksmith", tmp_path)  # 6 already waiting: it waits its turn
    assert T.make_room_for_review("salema", tmp_path)          # 1 waiting: still rare
    assert len(list(tmp_path.glob("*.json"))) == 7


def test_old_embeddings_of_another_size_are_left_out(tmp_path, monkeypatch):
    """Regression: one early picture's embedding came from BioCLIP 2 (768 numbers) without saying so,
    and the camera classifier's nightly training crashed on the mix."""
    from ml import train_classifier as tc
    monkeypatch.setattr(tc, "REVIEW", tmp_path)
    monkeypatch.setattr(tc, "DECISIONS", tmp_path / "decisions.csv")
    monkeypatch.setattr(tc, "current_embedding_model", lambda: "bioclip-2.5")
    (tmp_path / "approved").mkdir()
    rows = ["reviewed_at,taken_at,image,kind,logged_as,decision,common_name,scientific_name,category,best_guess,"
            "best_guess_prob,reviewer"]
    for i, (size, model) in enumerate([(1024, "bioclip-2.5"), (1024, "bioclip-2.5"), (768, None), (1024, None)]):
        card = {"embedding": [0.1] * size, **({"embedding_model": model} if model else {})}
        (tmp_path / "approved" / f"c{i}.json").write_text(json.dumps(card), encoding="utf-8")
        rows.append(f'"x","t","c{i}.jpg","uncertain","","approved","blacksmith","","","blacksmith","0.5","person"')
    (tmp_path / "decisions.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    X, y, _ = tc.examples()
    assert X.shape == (3, 1024) and len(y) == 3
