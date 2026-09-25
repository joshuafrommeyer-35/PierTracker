"""The tracker's SQLite database: data/piertracker.db.

One file holds everything, so questions that need several kinds of data ("which hours had kelp
bass while the water was 2 C above normal?") are a single SQL query. The CSVs stay, for the
public repo; this is the place to explore.

Tables (see SCHEMA below for every column):
  species     the animals the tracker knows (from species.json), with their look-alike group
  snapshots   every frame the tracker analyzed: one row per ~10 s while the cam streams
  sightings   what was in a snapshot: one row per animal type per snapshot
  conditions  hourly conditions at the pier (tide, temperatures, El Nino index, ...)
  reviews     answers given in the review window
  visits      fish followed across frames: one row per visit (arrival to leaving), named from all looks

There are deliberately no views yet: building them (e.g. sightings joined with that hour's
conditions) is part of the SQL walkthrough (docs/SQL_WALKTHROUGH.md).

Commands:
  python db.py sync      refresh species, conditions and reviews from their files (nightly does this)
  python db.py sandbox   copy the database to sandbox/piertracker_sandbox.db to practice on
"""

import csv
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

TRACKER = Path(__file__).resolve().parent
LIVECAMS = TRACKER.parent
DB_PATH = LIVECAMS / "data" / "piertracker.db"
SANDBOX = LIVECAMS / "sandbox" / "piertracker_sandbox.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS species (
    common_name      TEXT PRIMARY KEY,
    scientific_name  TEXT,
    category         TEXT,     -- fish, shark/ray, mammal, invertebrate, bird, person
    look_alike_group TEXT,     -- e.g. 'silversides & sardines' (NULL if none)
    scene            INTEGER   -- 1 = also looked for outside the fish detector (octopus, crabs...)
);

CREATE TABLE IF NOT EXISTS snapshots (
    taken_at  TEXT PRIMARY KEY,  -- local time, e.g. '2026-09-25T10:15:30'
    date      TEXT NOT NULL,     -- '2026-09-25'
    hour      INTEGER NOT NULL,  -- 0-23
    dark       INTEGER NOT NULL,  -- 1 = night frame: counted, but no model ran
    murky      INTEGER NOT NULL DEFAULT 0,  -- 1 = water too murky to identify anything: no model ran
    visibility REAL               -- how much detail the frame shows (higher = clearer water)
);

CREATE TABLE IF NOT EXISTS sightings (
    id           INTEGER PRIMARY KEY,
    taken_at     TEXT NOT NULL REFERENCES snapshots(taken_at),
    common_name  TEXT NOT NULL,  -- an animal, a look-alike group, 'small fish' or 'fish (unidentified)'
    is_school    INTEGER NOT NULL DEFAULT 0,  -- 1 = five or more at once
    count        INTEGER NOT NULL,            -- for a school of small fish: a rough estimate
    confidence   REAL,
    method       TEXT            -- zero-shot | camera-trained | detector only
);
CREATE INDEX IF NOT EXISTS sightings_by_time ON sightings(taken_at);
CREATE INDEX IF NOT EXISTS sightings_by_animal ON sightings(common_name);

CREATE TABLE IF NOT EXISTS visits (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT,     -- first frame the fish was seen in
    ended_at    TEXT,     -- last frame
    looks       INTEGER,  -- frames it was seen in (regular + burst)
    common_name TEXT,     -- named from the average of all looks
    confidence  REAL
);

CREATE TABLE IF NOT EXISTS conditions (
    date TEXT, hour INTEGER,
    tide_predicted_m REAL, water_level_m REAL, tide_trend INTEGER,  -- +1 rising, -1 falling
    noaa_water_temp_c REAL, air_temp_c REAL, wind_speed_ms REAL, wind_gust_ms REAL,
    wind_dir_deg REAL, air_pressure_hpa REAL,
    pier_water_temp_c REAL, pier_temp_anomaly_c REAL,  -- vs. the normal for the date
    salinity_psu REAL, oxygen_mg_l REAL, ph REAL, turbidity_ntu REAL, chlorophyll_ug_l REAL,
    oni REAL,                                            -- El Nino index
    PRIMARY KEY (date, hour)
);

CREATE TABLE IF NOT EXISTS reviews (
    reviewed_at TEXT, taken_at TEXT, image TEXT PRIMARY KEY,
    kind TEXT,          -- uncertain | check
    logged_as TEXT,     -- for checks: the name the tracker logged
    decision TEXT,      -- approved | rejected
    answer TEXT,        -- the animal the person picked (empty when rejected)
    tracker_best_guess TEXT, tracker_best_guess_prob REAL
);
"""


def connect(path: Path = None) -> sqlite3.Connection:
    path = path or DB_PATH  # looked up at call time, so tests can point DB_PATH elsewhere
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")  # readers (you, exploring) never block the tracker's writes
    con.executescript(SCHEMA)
    return con


def record_snapshot(con: sqlite3.Connection, taken_at: str, dark: bool, sightings, murky=False, visibility=None):
    """Called by the tracker for every analyzed frame."""
    date, time = taken_at.split("T")
    con.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (taken_at, date, int(time[:2]), int(dark), int(murky),
                 None if visibility is None else round(visibility, 3)))
    con.executemany(
        "INSERT INTO sightings (taken_at, common_name, is_school, count, confidence, method) VALUES (?, ?, ?, ?, ?, ?)",
        [(taken_at, s.common.removesuffix(" (school)"), int(s.common.endswith(" (school)")), s.count,
          round(s.confidence, 3), s.method) for s in sightings])
    con.commit()


def record_visit(con: sqlite3.Connection, started_at: str, ended_at: str, looks: int, common_name: str,
                 confidence: float):
    """One fish followed across frames, from arrival to leaving."""
    con.execute("INSERT INTO visits (started_at, ended_at, looks, common_name, confidence) VALUES (?, ?, ?, ?, ?)",
                (started_at, ended_at, looks, common_name, round(confidence, 3)))
    con.commit()


def sync(con: sqlite3.Connection):
    """Refreshes the tables that come from other files."""
    spec = json.loads((TRACKER / "species.json").read_text(encoding="utf-8"))
    con.execute("DELETE FROM species")
    con.executemany("INSERT INTO species VALUES (?, ?, ?, ?, ?)",
                    [(s["common"], s["scientific"] or None, s["category"], s.get("group"), int(bool(s.get("scene"))))
                     for s in spec["species"]])

    conditions = LIVECAMS / "data" / "environment_hourly.csv"
    if conditions.exists():
        columns = [r[1] for r in con.execute("PRAGMA table_info(conditions)")]
        with conditions.open(encoding="utf-8") as f:
            rows = [[r.get(c) or None for c in columns] for r in csv.DictReader(f)]
        con.executemany(f"INSERT OR REPLACE INTO conditions VALUES ({', '.join('?' * len(columns))})", rows)

    decisions = LIVECAMS / "data" / "review" / "decisions.csv"
    if decisions.exists():
        with decisions.open(encoding="utf-8") as f:
            con.executemany("INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (r["reviewed_at"], r["taken_at"], r["image"], r.get("kind") or "uncertain", r.get("logged_as") or None,
                 r["decision"], r["common_name"] or None, r["best_guess"] or None,
                 float(r["best_guess_prob"]) if r["best_guess_prob"] else None)
                for r in csv.DictReader(f)])
    con.commit()


def backfill_sightings(con: sqlite3.Connection):
    """One-time import of data/sightings.csv rows logged before the database existed. Only frames with
    sightings were recorded back then, so those hours have no complete snapshot count."""
    path = LIVECAMS / "data" / "sightings.csv"
    if not path.exists() or con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]:
        return 0
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        con.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, 0, 0, NULL)",
                    (r["timestamp"], r["date"], int(r["time"][:2])))
        name = r["common_name"]
        con.execute("INSERT INTO sightings (taken_at, common_name, is_school, count, confidence, method) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (r["timestamp"], name.removesuffix(" (school)"),
                                                  int(name.endswith(" (school)")), int(r["count"]),
                                                  float(r["confidence"]), r.get("method") or None))
    con.commit()
    return len(rows)


def make_sandbox():
    """A copy to practice on: break it freely, then run this again for a fresh one."""
    SANDBOX.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as live, closing(sqlite3.connect(SANDBOX)) as copy:
        live.backup(copy)  # a consistent copy even while the tracker is writing
    return SANDBOX


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if command == "sync":
        with connect() as con:
            sync(con)
        print(f"synced {DB_PATH}")
    elif command == "sandbox":
        print(f"fresh sandbox: {make_sandbox()}")
    else:
        print(__doc__)
