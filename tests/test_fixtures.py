"""Telling fixed structure from animals: the long-term background, the gallery of person-confirmed
fixtures and animals, and the rules that combine them."""
import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

import tracker as T

FRAME = Image.new("RGB", (1920, 1080))
STRAND = (1290, 400, 1340, 520)       # a small crop at a fixture's place
DEN = (1600, 0, 1760, 180)            # where the lobster lives


def unit(seed, dim=1024):
    v = np.random.default_rng(seed).normal(size=dim)
    return v / np.linalg.norm(v)


@pytest.fixture
def gallery(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "FIXTURE_GALLERY", tmp_path / "fixture_gallery.npz")
    monkeypatch.setattr(T, "REVIEWED", {"rejected": tmp_path / "rejected", "approved": tmp_path / "approved"})
    return T.FixtureGallery("model-a")


def test_gallery_saves_and_reloads_and_saves_again(gallery, tmp_path):
    """Regression: the saved file stayed open while loading, and loading also saves new review answers,
    so Windows couldn't replace the file and every frame failed until restart."""
    gallery.add(T.square_box(FRAME, STRAND), unit(1), animal=False, source="a.json")
    gallery.save()
    (tmp_path / "rejected").mkdir()
    card = {"embedding_model": "model-a", "embedding": unit(2).tolist(), "box": list(DEN)}
    (tmp_path / "rejected" / "b.json").write_text(json.dumps(card), encoding="utf-8")
    again = T.FixtureGallery("model-a")  # loads the file, then saves the new answer while loading
    assert len(again.embs) == 2
    with np.load(T.FIXTURE_GALLERY) as saved:  # and that save really reached the disk
        assert len(saved["embs"]) == 2
    assert len(T.FixtureGallery("another-model").embs) == 0  # embeddings from another model are ignored


def test_review_answers_feed_the_gallery(gallery, tmp_path):
    (tmp_path / "rejected").mkdir()
    card = {"embedding_model": "model-a", "embedding": unit(3).tolist(), "box": list(STRAND)}
    (tmp_path / "rejected" / "20260925-100000_leopard-shark.json").write_text(json.dumps(card), encoding="utf-8")
    gallery.add_reviewed()
    assert len(gallery.embs) == 1 and gallery.animal == [False]
    gallery.add_reviewed()
    assert len(gallery.embs) == 1  # each answer is added once


def test_shortcut_only_at_confirmed_fixtures_without_animals(gallery):
    gallery.add(T.square_box(FRAME, STRAND), unit(1), animal=False, source="strand")
    assert gallery.known_place(FRAME, STRAND)
    assert not gallery.known_place(FRAME, (100, 100, 150, 180))       # somewhere else
    assert not gallery.known_place(FRAME, (1100, 0, 1900, 900))       # big crop: always gets a full look
    gallery.add(T.square_box(FRAME, STRAND), unit(9), animal=True, source="lobster")
    assert not gallery.known_place(FRAME, STRAND)                     # a confirmed animal lives here too


def test_structure_rules():
    assert T.is_structure((0.80, 0.0), -1.0)        # looks just like a confirmed fixture
    assert T.is_structure((0.70, 0.0), 0.82)        # fairly like one, and like the background
    assert not T.is_structure((0.70, 0.0), 0.60)    # fairly like one, but not like the background
    assert not T.is_structure((0.80, 0.85), 0.95)   # more like a confirmed animal: the lobster stays
    assert not T.is_structure((0.0, 0.0), 0.99)     # nothing confirmed here: never auto-ignored


def test_background_correlation_follows_sway_but_not_new_things(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "BACKGROUND_IMAGE", tmp_path / "bg.png")
    scene = Image.new("RGB", (1920, 1080), (20, 120, 140))
    draw = ImageDraw.Draw(scene)
    draw.line((1310, 380, 1300, 540), fill=(10, 40, 50), width=14)       # the hanging strand
    bg = T.Background()
    t0 = T.datetime(2026, 9, 25, 10)
    for i in range(4):
        bg.add(scene, t0 + i * T.BACKGROUND_EVERY)
    assert bg.correlation(scene, STRAND) > 0.95
    swayed = Image.new("RGB", scene.size, (20, 120, 140))
    ImageDraw.Draw(swayed).line((1312, 380, 1302, 540), fill=(10, 40, 50), width=14)  # moved 2 px
    assert bg.correlation(swayed, STRAND) > 0.85
    fish = scene.copy()
    ImageDraw.Draw(fish).ellipse((1280, 430, 1350, 470), fill=(200, 190, 60))        # something new in front
    assert bg.correlation(fish, STRAND) < bg.correlation(scene, STRAND)


def test_reference_classifier_mix_is_a_distribution():
    names = ["kelp bass", "blacksmith", "murky green water"]
    negative = np.array([False, False, True])
    zs = np.array([0.5, 0.3, 0.2])
    classes = ["blacksmith", "kelp bass", "not an animal"]
    p = T.combine_reference(zs, names, negative, np.array([0.8, 0.1, 0.1]), classes, 0.75)
    assert abs(p.sum() - 1) < 1e-9
    assert p[1] > p[0]  # the classifier's strong "blacksmith" wins over zero-shot's lean to kelp bass
    confident_bg = T.combine_reference(zs, names, negative, np.array([0.05, 0.05, 0.9]), classes, 0.75)
    assert confident_bg[2] > p[2]  # its "not an animal" vote raises the non-animal share


def test_square_crop_stays_inside_the_frame():
    x0, y0, x1, y1 = T.square_box(FRAME, (1880, 1040, 1920, 1080))
    assert x0 >= 0 and y0 >= 0 and x1 <= 1920 and y1 <= 1080 and x1 - x0 == y1 - y0
