"""Water clarity: judged by how well the pilings stand out, so murk counts and dim light doesn't; what
poor visibility changes in the statistics; and the database's estimate for older snapshots."""
import json
from contextlib import closing
from dataclasses import dataclass

import numpy as np
from PIL import Image

import db
import publish_results as pr
import tracker as T


@dataclass
class Sighting:
    common: str
    count: int = 1
    confidence: float = 0.9
    method: str = "zero-shot"


def pier(murk=0.0, light=1.0):
    """The camera's view, simplified: blue-green water with the far pilings, the middle piling and the
    nearest one at their places. Murk blends each piling toward the water colour, the farther the more."""
    water = np.array([30, 120, 150], np.float32)
    img = np.zeros((1080, 1920, 3), np.float32) + water
    for (x0, x1), darkness, distance in (((320, 700), 0.45, 1.0), ((1340, 1600), 0.55, 0.5), ((1780, 1920), 0.8, 0.1)):
        seen = (1 - murk) ** (3 * distance)  # contrast fades faster with distance
        img[:, x0:x1] = water * (1 - darkness * seen)
    return Image.fromarray((img * light).clip(0, 255).astype(np.uint8))


def level(img):
    return T.clarity_level(*T.landmark_contrast(img))


def test_clarity_falls_with_murk():
    assert [level(pier(m)) for m in (0.0, 0.25, 0.55, 0.8)] == ["good", "fair", "poor", "very poor"]


def test_dim_light_isnt_murk():
    """The fine-detail score drops at dusk; the pilings' contrast against the water doesn't."""
    for murk in (0.0, 0.25, 0.55):
        assert level(pier(murk, light=0.4)) == level(pier(murk))


def test_a_moved_camera_falls_back_on_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "BACKGROUND_IMAGE", tmp_path / "none.png")
    background = T.Background()
    assert background.landmarks_ok  # no background yet: assume the pilings are in place
    background.median = pier().convert("L")
    assert background.landmarks_ok
    background.median = Image.new("L", (960, 540), 110)  # no pilings where they should be
    assert not background.landmarks_ok
    assert [T.detail_level(d) for d in (2.2, 1.3, 0.7, 0.3)] == ["good", "fair", "poor", "very poor"]


def test_the_database_uses_the_trackers_fallback_cutoffs():
    assert db.DETAIL_CUTS == tuple(cut for cut, _ in T.DETAIL_LEVELS)


def test_older_snapshots_get_a_clarity_estimate(tmp_path):
    path = tmp_path / "old.db"
    with closing(db.connect(path)) as con:
        con.execute("INSERT INTO snapshots (taken_at, date, hour, dark, visibility) VALUES "
                    "('2026-09-29T11:00:00', '2026-09-29', 11, 0, 0.73), ('2026-09-26T11:00:00', '2026-09-26', 11, 0, 2.2), "
                    "('2026-09-26T03:00:00', '2026-09-26', 3, 1, NULL)")
        con.commit()
    with closing(db.connect(path)) as con:  # the estimate is filled in on connecting
        got = dict(con.execute("SELECT taken_at, clarity FROM snapshots"))
    assert got == {"2026-09-29T11:00:00": "poor", "2026-09-26T11:00:00": "good", "2026-09-26T03:00:00": None}


def test_poor_visibility_is_counted_and_shown(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "STATS_FROM", "2026-01-01T00:00:00")
    with closing(db.connect()) as con:
        for i in range(10):  # a murky hour: 8 poor snapshots, 2 too murky to see
            db.record_snapshot(con, f"2026-09-29T11:00:{i:02d}", False, [Sighting("sea basses")] if i == 0 else [],
                               murky=i >= 8, clarity="very poor" if i >= 8 else "poor")
        for i in range(10):  # a fair hour with a kelp bass
            db.record_snapshot(con, f"2026-09-29T15:00:{i:02d}", False, [Sighting("kelp bass")], clarity="fair")
    effort, species, rows = pr.load_hourly()
    assert effort[("2026-09-29", 11)] == (10, 0, 2, 8)
    days, by_hour = pr.build(effort, species)
    assert pr.visibility_summary(days["2026-09-29"])[0] == "poor"  # 10 of 20 daylight snapshots poor or worse
    text = pr.render(days, by_hour, {}, {}, 0, [], pr.load_encounters(effort))
    assert "| ...of it in poor visibility: fish counted, not named |" in text
    # kelp bass is a species: in poor water it couldn't have been named, so its share is of the fair hour only
    assert "| kelp bass | fish | 1 | 10 | 100.00% |" in text
    # a look-alike group is logged in poor water too: its share is of every snapshot it could be seen in
    assert pr.logged_in_poor_water("sea basses") and not pr.logged_in_poor_water("kelp bass")


def test_species_models_dont_count_poor_water_as_looking(tmp_path, monkeypatch):
    """Otherwise murky water would look like it drives every named fish away."""
    from ml import conditions_model as cm
    conditions = tmp_path / "environment_hourly.csv"
    conditions.write_text("date,hour,turbidity_ntu\n2026-09-29,11,1.3\n2026-09-29,15,0.5\n", encoding="utf-8")
    monkeypatch.setattr(cm, "CONDITIONS", conditions)
    hour = {"date": "2026-09-29", "snapshots_analyzed": 300, "snapshots_dark": 0, "snapshots_murky": 0,
            "common_name": "", "snapshots_seen": 0, "max_count": 0}
    table, _ = cm.load([{**hour, "hour": 11, "snapshots_poor": 280}, {**hour, "hour": 15, "snapshots_poor": 0}])
    assert list(table.daylight) == [300, 300] and list(table.naming) == [20, 300]
    assert cm.logged_in_poor_water("small fish (school)") and cm.logged_in_poor_water("sea basses")
    assert not cm.logged_in_poor_water("kelp bass")
