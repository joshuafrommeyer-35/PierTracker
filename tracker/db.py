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
from datetime import datetime
import sys
from contextlib import closing
from pathlib import Path

TRACKER = Path(__file__).resolve().parent
LIVECAMS = TRACKER.parent
DB_PATH = LIVECAMS / "data" / "piertracker.db"
# The statistics start here. Earlier snapshots were named by an older model (BioCLIP 2); they stay in
# the database but are left out of the published numbers and the models.
STATS_FROM = "2026-09-25T10:01:00"
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
    method       TEXT,           -- zero-shot | camera-trained | detector only
    corrected_name TEXT          -- from a review answer: the right animal, '' = not an animal, NULL = as logged
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
    tracker_best_guess TEXT, tracker_best_guess_prob REAL,
    reviewer TEXT       -- person (the review window) | claude (a structure/empty-water check by eye)
);
"""


def connect(path: Path = None) -> sqlite3.Connection:
    path = path or DB_PATH  # looked up at call time, so tests can point DB_PATH elsewhere
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")  # readers (you, exploring) never block the tracker's writes
    con.executescript(SCHEMA)
    migrate(con)
    return con


def migrate(con: sqlite3.Connection):
    """Columns added after a database was first made."""
    for table, column in (("sightings", "corrected_name TEXT"), ("reviews", "reviewer TEXT")):
        if column.split()[0] not in [c[1] for c in con.execute(f"PRAGMA table_info({table})")]:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
    con.commit()


def apply_corrections(con: sqlite3.Connection, corrections: Path = None):
    """Corrections from checking the pictures. The logged name stays in common_name; statistics use
    corrected_name ('' = not an animal). Returns how many sightings changed.

    1. data/corrections.csv: sightings checked by eye outside the review window (columns from, to,
       logged_as, corrected_to, reviewer, reason; from = to for a single sighting).
    2. Answers to "is this right?" pictures in the review window, applied last and a person's after
       Claude's, so a person's answer always wins."""
    fixed = 0
    corrections = corrections or LIVECAMS / "data" / "corrections.csv"
    if corrections.exists():
        with corrections.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                fixed += con.execute("UPDATE sightings SET corrected_name = ? WHERE taken_at BETWEEN ? AND ? "
                                     "AND common_name = ? AND corrected_name IS NOT ?",
                                     (r["corrected_to"], r["from"], r["to"], r["logged_as"], r["corrected_to"])).rowcount
    rows = con.execute("SELECT taken_at, logged_as, decision, answer FROM reviews WHERE kind = 'check' "
                       "ORDER BY CASE reviewer WHEN 'person' THEN 1 ELSE 0 END").fetchall()
    for taken_at, logged_as, decision, answer in rows:
        name = (answer or "") if decision == "approved" else ""
        if not logged_as or name == logged_as:
            continue  # right as logged
        fixed += con.execute("UPDATE sightings SET corrected_name = ? WHERE taken_at = ? AND common_name = ? "
                             "AND corrected_name IS NOT ?", (name, taken_at, logged_as, name)).rowcount
    con.commit()
    return fixed


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
            con.executemany("INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (r["reviewed_at"], r["taken_at"], r["image"], r.get("kind") or "uncertain", r.get("logged_as") or None,
                 r["decision"], r["common_name"] or None, r["best_guess"] or None,
                 float(r["best_guess_prob"]) if r["best_guess_prob"] else None, r.get("reviewer") or "person")
                for r in csv.DictReader(f)])
    con.commit()
    apply_corrections(con)


def hourly(con: sqlite3.Connection):
    """The hourly summary: one row per (date, hour, animal), with that hour's effort (snapshots
    analyzed, dark, murky) and the animal's snapshots and most at once. An hour with no sightings
    gets one row with a blank animal. Schools are named "<animal> (school)"."""
    effort = {(d, h): (n, dark, murky) for d, h, n, dark, murky in con.execute(
        "SELECT date, hour, COUNT(*), SUM(dark), SUM(murky) FROM snapshots WHERE taken_at >= ? "
        "GROUP BY date, hour", (STATS_FROM,))}
    seen = {}
    for d, h, name, n, most in con.execute(
            "SELECT p.date, p.hour, COALESCE(g.corrected_name, g.common_name) "
            "|| CASE WHEN g.is_school THEN ' (school)' ELSE '' END, COUNT(DISTINCT g.taken_at), MAX(g.count) "
            "FROM sightings g JOIN snapshots p ON p.taken_at = g.taken_at "
            "WHERE p.taken_at >= ? AND COALESCE(g.corrected_name, g.common_name) <> '' GROUP BY 1, 2, 3",
            (STATS_FROM,)):
        seen.setdefault((d, h), []).append((name, n, most))
    rows = []
    for (d, h), (n, dark, murky) in sorted(effort.items()):
        for name, s, most in sorted(seen.get((d, h), [])) or [("", 0, 0)]:
            rows.append({"date": d, "hour": h, "snapshots_analyzed": n, "snapshots_dark": dark or 0,
                         "snapshots_murky": murky or 0, "common_name": name, "snapshots_seen": s, "max_count": most})
    return rows


def sighting_times(con: sqlite3.Connection):
    """(time, animal) of every sighting since STATS_FROM, in time order: for counting encounters."""
    return [(datetime.fromisoformat(t), name) for t, name in con.execute(
        "SELECT taken_at, COALESCE(corrected_name, common_name) || CASE WHEN is_school THEN ' (school)' ELSE '' END "
        "FROM sightings WHERE taken_at >= ? AND COALESCE(corrected_name, common_name) <> '' ORDER BY taken_at",
        (STATS_FROM,))]


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
