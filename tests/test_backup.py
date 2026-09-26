"""The nightly Google Drive backup: never half-written, and never overwriting a bigger backup."""
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import db
import nightly


def make_db(path: Path, snapshots: int):
    with closing(db.connect(path)) as con:
        for i in range(snapshots):
            db.record_snapshot(con, f"2026-09-25T10:00:{i:02d}", False, [])


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A LiveCams folder with a database of 5 snapshots, a CSV, a review picture and a frame-bank frame."""
    data = tmp_path / "LiveCams" / "data"
    (data / "review" / "approved").mkdir(parents=True)
    (data / "frame_bank" / "2026-09-25").mkdir(parents=True)
    (data / "environment_hourly.csv").write_text("date,hour\n", encoding="utf-8")
    (data / "review" / "approved" / "a.json").write_text("{}", encoding="utf-8")
    (data / "frame_bank" / "2026-09-25" / "100000.jpg").write_bytes(b"jpg")
    monkeypatch.setattr(nightly, "LIVECAMS", tmp_path / "LiveCams")
    monkeypatch.setattr(db, "DB_PATH", data / "piertracker.db")
    make_db(data / "piertracker.db", 5)
    return tmp_path


def test_backup_copies_everything(live):
    target = live / "Drive"
    nightly.backup(target)
    with closing(sqlite3.connect(target / "piertracker.db")) as con:
        assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 5
    assert (target / "environment_hourly.csv").exists()
    assert (target / "review" / "approved" / "a.json").exists()
    assert (target / "frame_bank" / "2026-09-25" / "100000.jpg").exists()
    assert not list(target.rglob("*.partial"))  # every file swapped in whole


def test_backup_is_one_self_contained_file(live):
    target = live / "Drive"
    nightly.backup(target)
    nightly.backup(target)  # the second run reads the first copy (the size check)
    assert not (target / "piertracker.db-wal").exists() and not (target / "piertracker.db-shm").exists()
    with closing(sqlite3.connect(target / "piertracker.db")) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_backup_never_overwrites_a_bigger_backup(live):
    target = live / "Drive"
    target.mkdir()
    make_db(target / "piertracker.db", 9)  # e.g. a fresh install that wasn't restored first
    with pytest.raises(RuntimeError, match="restore it first"):
        nightly.backup(target)
    with closing(sqlite3.connect(target / "piertracker.db")) as con:
        assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 9


def test_copy_safely_keeps_the_old_file_if_the_copy_fails(tmp_path, monkeypatch):
    dest = tmp_path / "file.csv"
    dest.write_text("old", encoding="utf-8")
    src = tmp_path / "new.csv"
    src.write_text("new", encoding="utf-8")

    def broken_copy(a, b):
        Path(b).write_text("ha", encoding="utf-8")
        raise OSError("disk went away")
    monkeypatch.setattr(nightly.shutil, "copy2", broken_copy)
    with pytest.raises(OSError):
        nightly.copy_safely(src, dest)
    assert dest.read_text(encoding="utf-8") == "old"
