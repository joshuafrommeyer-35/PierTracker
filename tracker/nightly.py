"""The once-a-day job the tracker starts at the first frame after midnight (idle priority).

  1. Fetch the pier's conditions (environment.py), and copy them, the species list and the
     review answers into the SQLite database (db.py).
  2. Retrain the camera classifier from the review window's answers (ml/train_classifier.py).
  3. Refit "what brings animals in" (ml/conditions_model.py), once there's enough data.
  4. With --publish: update the README's results and push them (publish_results.py).
  5. With --backup <folder> (e.g. on Google Drive): copy what can't be recreated there: the database,
     the review answers, the raw CSVs, and the frame bank (kept there for good; the PC keeps 90 days).

Each step runs even if an earlier one fails, and failures go to logs/nightly.log.
"""

import logging
import logging.handlers
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

LIVECAMS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


def copy_safely(src: Path, dest: Path):
    """Copies under a temporary name, then swaps it in: if the copy is interrupted, the previous
    backup of that file is still whole."""
    tmp = dest.with_name(dest.name + ".partial")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


def snapshot_count(path: Path) -> int:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        return con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]


def backup(target: Path):
    import db
    target.mkdir(parents=True, exist_ok=True)
    data = LIVECAMS / "data"
    # A fresh install that wasn't restored first has less than the backup: never overwrite the good
    # copy with it. setup.bat restores the backup when this PC has no database.
    kept = target / "piertracker.db"
    if kept.exists() and snapshot_count(kept) > snapshot_count(db.DB_PATH):
        raise RuntimeError(f"{kept} has more data than this PC; restore it first (setup.bat). Backup skipped.")
    # The database via SQLite's backup (consistent while the tracker writes), made locally first:
    # synced folders don't like SQLite writing to them directly.
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "piertracker.db"
        # closing(): sqlite3's own "with" commits but doesn't close, which leaves the file locked on Windows
        with closing(db.connect()) as live, closing(sqlite3.connect(copy_path)) as copy:
            live.backup(copy)
        copy_safely(copy_path, target / "piertracker.db")
    for csv_file in data.glob("*.csv"):
        copy_safely(csv_file, target / csv_file.name)
    if (data / "review").exists():
        shutil.copytree(data / "review", target / "review", dirs_exist_ok=True,
                        copy_function=lambda a, b: copy_safely(Path(a), Path(b)))
    # The frame bank only grows there: copy what's new, never delete.
    for frame in (data / "frame_bank").rglob("*.jpg"):
        dest = target / "frame_bank" / frame.relative_to(data / "frame_bank")
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy_safely(frame, dest)


def main(publish: bool, backup_dir: str = None):
    handler = logging.handlers.RotatingFileHandler(LIVECAMS / "logs" / "nightly.log", maxBytes=500_000,
                                                   backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(name)s %(message)s")
    log = logging.getLogger("nightly")

    def step(name, fn):
        try:
            fn()
            log.info("%s: done", name)
        except Exception:
            log.exception("%s failed", name)

    import db
    import environment
    step("conditions", environment.update)

    def sync_database():
        with db.connect() as con:
            db.sync(con)
    step("database", sync_database)
    from ml import conditions_model, train_classifier
    step("camera classifier", train_classifier.main)
    step("conditions model", conditions_model.main)
    if publish:
        import publish_results
        step("publish", lambda: publish_results.main(update_environment=False))
    if backup_dir:
        step("backup", lambda: backup(Path(backup_dir)))


if __name__ == "__main__":
    args = sys.argv[1:]
    main(publish="--publish" in args, backup_dir=args[args.index("--backup") + 1] if "--backup" in args else None)
