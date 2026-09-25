"""The database: migrations, the hourly summary the publisher and models read, and review corrections."""
import sqlite3
from contextlib import closing
from dataclasses import dataclass

import pytest

import db


@dataclass
class Sighting:
    common: str
    count: int = 1
    confidence: float = 0.9
    method: str = "zero-shot"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "STATS_FROM", "2026-01-01T00:00:00")
    with closing(db.connect(tmp_path / "t.db")) as c:
        yield c


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as c:
        c.executescript("""
            CREATE TABLE sightings (id INTEGER PRIMARY KEY, taken_at TEXT NOT NULL, common_name TEXT NOT NULL,
                                    is_school INTEGER NOT NULL DEFAULT 0, count INTEGER NOT NULL, confidence REAL, method TEXT);
            CREATE TABLE reviews (reviewed_at TEXT, taken_at TEXT, image TEXT PRIMARY KEY, kind TEXT, logged_as TEXT,
                                  decision TEXT, answer TEXT, tracker_best_guess TEXT, tracker_best_guess_prob REAL);""")
    with closing(db.connect(path)) as c:
        assert "corrected_name" in [r[1] for r in c.execute("PRAGMA table_info(sightings)")]
        assert "reviewer" in [r[1] for r in c.execute("PRAGMA table_info(reviews)")]


def test_hourly_summary_counts_snapshots_and_schools(con):
    db.record_snapshot(con, "2026-09-25T10:00:00", False, [Sighting("kelp bass"), Sighting("blacksmith (school)", 9)])
    db.record_snapshot(con, "2026-09-25T10:00:10", False, [Sighting("kelp bass")])
    db.record_snapshot(con, "2026-09-25T10:00:20", True, [])
    db.record_snapshot(con, "2026-09-25T11:00:00", False, [])
    rows = {(r["hour"], r["common_name"]): r for r in db.hourly(con)}
    assert rows[(10, "kelp bass")]["snapshots_seen"] == 2
    assert rows[(10, "blacksmith (school)")]["max_count"] == 9
    assert rows[(10, "kelp bass")]["snapshots_analyzed"] == 3 and rows[(10, "kelp bass")]["snapshots_dark"] == 1
    assert rows[(11, "")]["snapshots_seen"] == 0  # an hour with nothing seen still counts as effort


def test_statistics_start_at_stats_from(con, monkeypatch):
    db.record_snapshot(con, "2026-09-25T09:59:00", False, [Sighting("ocean whitefish")])
    db.record_snapshot(con, "2026-09-25T10:02:00", False, [Sighting("blacksmith")])
    monkeypatch.setattr(db, "STATS_FROM", "2026-09-25T10:01:00")
    names = {r["common_name"] for r in db.hourly(con)}
    assert "ocean whitefish" not in names and "blacksmith" in names


def test_review_answers_correct_the_logged_sighting(con):
    db.record_snapshot(con, "2026-09-25T13:04:30", False, [Sighting("diamond stingray")])
    db.record_snapshot(con, "2026-09-25T11:59:12", False, [Sighting("market squid")])
    con.executemany("INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        ("x", "2026-09-25T13:04:30", "a.jpg", "check", "diamond stingray", "approved", "California spiny lobster", "", 0.9, "person"),
        ("x", "2026-09-25T11:59:12", "b.jpg", "check", "market squid", "rejected", None, "", 0.9, "person")])
    assert db.apply_corrections(con) == 2
    names = {r["common_name"] for r in db.hourly(con)}
    assert "California spiny lobster" in names and "diamond stingray" not in names
    assert "market squid" not in names  # "not an animal" drops out (its hour still counts as effort)
    assert [n for _, n in db.sighting_times(con)] == ["California spiny lobster"]
    # the logged name is kept
    assert con.execute("SELECT common_name FROM sightings WHERE corrected_name = ''").fetchone()[0] == "market squid"
    assert db.apply_corrections(con) == 0  # running it again changes nothing
