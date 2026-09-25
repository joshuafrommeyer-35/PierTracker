"""Publishes the tracker's summary stats to the project's GitHub repo.

Fetches the day's conditions at the pier (environment.py), rolls data/hourly_summary.csv up
into results/daily_summary.csv, publishes the hourly sightings and conditions side by side
(results/hourly_summary.csv, results/environment_hourly.csv; they join on date + hour),
rewrites the "Tracking results" section of README.md, and commits and pushes if anything
changed. The tracker runs this once a day when started with --publish; you can
also run it by hand. Only summaries are published: the raw per-snapshot
sightings and the cam images stay on this PC.
"""

import csv
import json
import logging
import re
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import environment

ROOT = Path(__file__).resolve().parent.parent
HOURLY_CSV = ROOT / "data" / "hourly_summary.csv"
SIGHTINGS_CSV = ROOT / "data" / "sightings.csv"
RESULTS = ROOT / "results"
DAILY_CSV = RESULTS / "daily_summary.csv"
VALIDATION_CSV = RESULTS / "validation.csv"
CROPS = ROOT / "data" / "crops"
DECISIONS_CSV = ROOT / "data" / "review" / "decisions.csv"
CONFIRMED_CSV = RESULTS / "confirmed_by_hand.csv"
ENV_CSV = environment.ENV_CSV
CLASSIFIER_REPORT = ROOT / "data" / "ml" / "classifier_report.md"
README = ROOT / "README.md"
SPECIES = ROOT / "tracker" / "species.json"
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
SNAPSHOT_SECONDS = 10  # LiveCams saves a frame this often (captureEverySeconds)
# Sightings of the same animal closer together than this are one encounter: the usual camera-trap
# rule for "independent detections" (results were found stable between 5 and 60 minutes).
ENCOUNTER_GAP = timedelta(minutes=30)
NO_ENCOUNTERS = ("small fish", "small fish (school)", "fish (unidentified)")  # a mix of kinds, not one animal
NO_WINDOW = 0x08000000


def load_hourly():
    effort = {}                                    # (date, hour) -> (analyzed, dark, murky)
    species = defaultdict(lambda: [0, 0])          # (date, hour, name) -> [snapshots seen, max count]
    if not HOURLY_CSV.exists():
        return effort, species
    with HOURLY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["date"], int(row["hour"]))
            effort[key] = (int(row["snapshots_analyzed"]), int(row["snapshots_dark"]),
                           int(row.get("snapshots_murky") or 0))
            if row["common_name"]:
                s = species[key + (row["common_name"],)]
                s[0] += int(row["snapshots_seen"])
                s[1] = max(s[1], int(row["max_count"]))
    return effort, species


def load_encounters(effort):
    """Encounters (independent detections) per (date, animal) from the per-snapshot sightings: a
    sighting starts a new encounter only if that animal wasn't seen in the previous 30 minutes. So
    a kelp bass that hangs around the camera for an hour is one encounter, not 360 snapshots. Only
    hours already in the hourly summary count, so this matches the rest of the numbers."""
    times = defaultdict(list)
    if SIGHTINGS_CSV.exists():
        with SIGHTINGS_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                times[row["common_name"]].append(datetime.fromisoformat(row["timestamp"]))
    encounters = defaultdict(int)
    for name, stamps in times.items():
        last = None
        for t in sorted(stamps):
            if (last is None or t - last >= ENCOUNTER_GAP) and (t.date().isoformat(), t.hour) in effort:
                encounters[(t.date().isoformat(), name)] += 1
            last = t
    return encounters


def categories():
    spec = json.loads(SPECIES.read_text(encoding="utf-8"))
    cats = {s["common"]: s["category"] for s in spec["species"]}
    cats.update({s["group"]: s["category"] for s in spec["species"] if s.get("group")})  # look-alike groups
    cats["fish (unidentified)"] = cats["small fish"] = "fish"
    return cats


def base_name(name):
    """ "blacksmith (school)" -> "blacksmith" """
    return name.removesuffix(" (school)")


def file_safe(name):
    return "".join(c if c.isalnum() else "-" for c in name)  # same as tracker.file_safe


def update_validation():
    """Tallies hand-checked sample crops into results/validation.csv and returns
    {animal: [reviewed, wrong]}. A day counts as reviewed once its data/crops/<date>
    folder has a "wrong" subfolder; the crops moved into it are the misidentified ones.
    Rows for reviewed days are kept after their crops are pruned."""
    rows = {}
    if VALIDATION_CSV.exists():
        with VALIDATION_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(r["date"], r["common_name"])] = [int(r["reviewed"]), int(r["wrong"])]
    names = {file_safe(n): n for n in categories()}
    for day in sorted(CROPS.glob("*")):
        wrong_dir = day / "wrong"
        if not wrong_dir.is_dir():
            continue
        rows = {k: v for k, v in rows.items() if k[0] != day.name}
        for crop in [*day.glob("*.jpg"), *wrong_dir.glob("*.jpg")]:
            name = names.get("_".join(crop.stem.split("_")[1:-1]))
            if name:
                tally = rows.setdefault((day.name, name), [0, 0])
                tally[0] += 1
                tally[1] += crop.parent == wrong_dir
    RESULTS.mkdir(exist_ok=True)
    with VALIDATION_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "common_name", "reviewed", "wrong"])
        for (d, name), (reviewed, wrong) in sorted(rows.items()):
            w.writerow([d, name, reviewed, wrong])
    totals = defaultdict(lambda: [0, 0])
    for (_, name), (reviewed, wrong) in rows.items():
        totals[name][0] += reviewed
        totals[name][1] += wrong
    return totals


def read_reviews():
    """What a person decided in the LiveCams review window (data/review/decisions.csv).

    Returns ({animal: [times confirmed, last date]}, number rejected) for "uncertain"
    sightings, and {animal: [checked, wrong]} for "check" samples of names the tracker
    logged. Publishes a copy of the decisions without local file names."""
    confirmed, rejected, checks, rows = defaultdict(lambda: [0, ""]), 0, defaultdict(lambda: [0, 0]), []
    if DECISIONS_CSV.exists():
        with DECISIONS_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                kind, logged = r.get("kind") or "uncertain", r.get("logged_as") or ""
                rows.append([r["taken_at"], kind, logged, r["decision"], r["common_name"],
                             r["best_guess"], r["best_guess_prob"]])
                if kind == "check":
                    tally = checks[logged]
                    tally[0] += 1
                    tally[1] += not (r["decision"] == "approved" and r["common_name"] == logged)
                elif r["decision"] == "approved":
                    c = confirmed[r["common_name"]]
                    c[0] += 1
                    c[1] = max(c[1], r["taken_at"][:10])
                else:
                    rejected += 1
    RESULTS.mkdir(exist_ok=True)
    with CONFIRMED_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["taken_at", "kind", "tracker_logged_as", "decision", "person_says", "tracker_best_guess",
                    "tracker_best_guess_prob"])
        w.writerows(sorted(rows))
    return confirmed, rejected, checks


def build(effort, species):
    days = defaultdict(lambda: {"analyzed": 0, "dark": 0, "murky": 0, "species": defaultdict(lambda: [0, 0])})
    for (d, _), (analyzed, dark, murky) in effort.items():
        days[d]["analyzed"] += analyzed
        days[d]["dark"] += dark
        days[d]["murky"] += murky
    by_hour_of_day = [0] * 24
    for (d, h, name), (seen, most) in species.items():
        s = days[d]["species"][name]
        s[0] += seen
        s[1] = max(s[1], most)
        by_hour_of_day[h] += seen
    return days, by_hour_of_day


def write_daily(days, encounters):
    RESULTS.mkdir(exist_ok=True)
    with DAILY_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "snapshots_analyzed", "snapshots_dark", "snapshots_murky", "common_name", "encounters",
                    "snapshots_seen", "max_count"])
        for d in sorted(days):
            day = days[d]
            rows = sorted(day["species"].items()) or [("", [0, 0])]
            for name, (seen, most) in rows:
                n = "" if not name or name in NO_ENCOUNTERS else encounters.get((d, name), 0)
                w.writerow([d, day["analyzed"], day["dark"], day["murky"], name, n, seen, most])


def render_validation(validation):
    lines = ["", "### Validation (hand-checked samples)", ""]
    if not validation:
        return lines + ["_No names have been checked by a person yet, so treat the names above as unverified "
                        "model guesses._"]
    reviewed = sum(r for r, _ in validation.values())
    wrong = sum(w for _, w in validation.values())
    lines += [f"{reviewed:,} of the tracker's names checked by a person so far; "
              f"{100 * (reviewed - wrong) / reviewed:.0f}% were named correctly.",
              "", "| Animal | Checked | Correct | Precision |", "|---|---:|---:|---:|"]
    for name, (r, w) in sorted(validation.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"| {name} | {r} | {r - w} | {100 * (r - w) / r:.0f}% |")
    return lines


def load_environment():
    if not ENV_CSV.exists():
        return {}
    with ENV_CSV.open(encoding="utf-8") as f:
        return {(r["date"], int(r["hour"])): r for r in csv.DictReader(f)}


DAILY_CONDITIONS_FIELDS = [
    "date", "water_temp_c", "water_temp_normal_c", "water_temp_anomaly_c", "water_temp_min_c", "water_temp_max_c",
    "turbidity_ntu_daytime", "chlorophyll_ug_l", "salinity_psu", "oxygen_mg_l", "ph", "tide_range_m",
    "air_temp_c", "wind_speed_ms", "el_nino_index_oni", "clear_snapshots", "murky_snapshots", "animal_snapshots"]


def daily_conditions(days, env):
    """One row per day: the pier's physical and chemical conditions next to that day's tracking effort
    and sightings. Days run from the first tracked day to the last day with conditions."""
    if not env or not days:
        return []
    try:
        normal = environment.climatology()
    except Exception:  # the normal is a bonus; the rest of the row stands without it
        normal = {}
    last = max(max(d for d, _ in env), max(days))
    rows = []
    day = date.fromisoformat(min(days))
    while day.isoformat() <= last:
        d = day.isoformat()

        def pick(column, hours=range(24)):
            return [float(env[(d, h)][column]) for h in hours if env.get((d, h), {}).get(column) not in (None, "")]

        def agg(vals, how, digits):
            return round(how(vals), digits) if vals else None

        temps, tide = pick("pier_water_temp_c"), pick("tide_predicted_m")
        norm = normal.get(day.timetuple().tm_yday)
        mean_temp = agg(temps, statistics.mean, 2)
        oni = pick("oni")
        tracked = days.get(d)
        rows.append({
            "date": d, "water_temp_c": mean_temp, "water_temp_normal_c": None if norm is None else round(norm, 2),
            "water_temp_anomaly_c": None if norm is None or mean_temp is None else round(mean_temp - norm, 2),
            "water_temp_min_c": agg(temps, min, 2), "water_temp_max_c": agg(temps, max, 2),
            "turbidity_ntu_daytime": agg(pick("turbidity_ntu", range(7, 19)), statistics.median, 2),
            "chlorophyll_ug_l": agg(pick("chlorophyll_ug_l"), statistics.mean, 2),
            "salinity_psu": agg(pick("salinity_psu"), statistics.mean, 2),
            "oxygen_mg_l": agg(pick("oxygen_mg_l"), statistics.mean, 2),
            "ph": agg(pick("ph"), statistics.mean, 3),
            "tide_range_m": round(max(tide) - min(tide), 2) if tide else None,
            "air_temp_c": agg(pick("air_temp_c"), statistics.mean, 1),
            "wind_speed_ms": agg(pick("wind_speed_ms"), statistics.mean, 1),
            "el_nino_index_oni": oni[-1] if oni else None,
            "clear_snapshots": tracked["analyzed"] - tracked["dark"] - tracked["murky"] if tracked else 0,
            "murky_snapshots": tracked["murky"] if tracked else 0,
            "animal_snapshots": sum(seen for seen, _ in tracked["species"].values()) if tracked else 0,
        })
        day += timedelta(days=1)
    return rows


def write_daily_conditions(rows):
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / "daily_conditions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, DAILY_CONDITIONS_FIELDS)
        w.writeheader()
        w.writerows(rows)


def render_conditions(rows):
    """Kept short on purpose: one headline, one chart, and the table folded away."""
    if not rows:
        return []

    def fmt(v, digits=1, signed=False):
        return "–" if v is None else (f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}")

    latest = next((r for r in reversed(rows) if r["water_temp_c"] is not None), rows[-1])
    oni = latest["el_nino_index_oni"]
    enso = ("" if oni is None else f" El Niño index **{oni:+.1f}**" +
            (" (El Niño)." if oni >= 0.5 else " (La Niña)." if oni <= -0.5 else " (neutral)."))
    lines = ["", "### Conditions at the pier", "",
             f"**{latest['date']}:** water {fmt(latest['water_temp_c'])} °C at ~5 m, "
             f"**{fmt(latest['water_temp_anomaly_c'], signed=True)} °C** vs. normal for the date. Turbidity "
             f"{fmt(latest['turbidity_ntu_daytime'], 2)} NTU, chlorophyll {fmt(latest['chlorophyll_ug_l'], 2)} µg/L."
             + enso, ""]
    chart = [r for r in rows if r["water_temp_c"] is not None][-30:]
    if len(chart) >= 2 and all(r["water_temp_normal_c"] is not None for r in chart):
        lo = min(min(r["water_temp_c"], r["water_temp_normal_c"]) for r in chart)
        hi = max(max(r["water_temp_c"], r["water_temp_normal_c"]) for r in chart)
        lines += ["```mermaid", "xychart-beta",
                  '    title "Water temperature at the pier vs. normal for the date (°C)"',
                  '    x-axis [' + ", ".join(f'"{r["date"][5:]}"' for r in chart) + "]",
                  f'    y-axis "°C" {int(lo) - 1} --> {int(hi) + 2}',
                  "    line [" + ", ".join(str(r["water_temp_c"]) for r in chart) + "]",
                  "    line [" + ", ".join(str(r["water_temp_normal_c"]) for r in chart) + "]",
                  "```", "",
                  "_Upper line: this year. Lower line: the 2013–2025 normal for each date._", ""]
    lines += ["<details><summary>Daily conditions, last 14 days</summary>", "",
              "| Date | Water °C | vs. normal | Turbidity (NTU) | Chlorophyll (µg/L) | Salinity | Oxygen (mg/L) | pH | Tide range (m) | Animal snapshots |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows[-14:]:
        lines.append(f"| {r['date']} | {fmt(r['water_temp_c'])} | {fmt(r['water_temp_anomaly_c'], signed=True)} | "
                     f"{fmt(r['turbidity_ntu_daytime'], 2)} | {fmt(r['chlorophyll_ug_l'], 2)} | {fmt(r['salinity_psu'], 2)} | "
                     f"{fmt(r['oxygen_mg_l'], 2)} | {fmt(r['ph'], 2)} | {fmt(r['tide_range_m'], 2)} | {r['animal_snapshots']:,} |")
    lines += ["", "Sources: SCCOOS shore station on the pier (water; quality-controlled readings only), NOAA La Jolla "
              "tide gauge, NOAA Oceanic Niño Index. Every day: [`results/daily_conditions.csv`](results/daily_conditions.csv); "
              "hourly, to join with the hourly sightings: [`results/environment_hourly.csv`](results/environment_hourly.csv).",
              "", "</details>"]
    return lines


def tracker_status():
    """When the tracker last looked at a frame, so a stalled stream or tracker is visible publicly."""
    try:
        import db
        with db.connect() as con:
            last = con.execute("SELECT MAX(taken_at) FROM snapshots").fetchone()[0]
    except Exception:
        return ""
    if not last:
        return ""
    age = datetime.now() - datetime.fromisoformat(last)
    if age > timedelta(hours=2):
        return f" ⚠️ **The tracker hasn't analyzed a frame since {last.replace('T', ' ')}.**"
    return f" Last frame analyzed {last.replace('T', ' ')}."


def render_confirmed(confirmed, rejected):
    if not confirmed and not rejected:
        return []
    lines = ["", "### Confirmed by hand", "",
             "Sightings the tracker wasn't sure about are saved for review. These were checked by a person: "
             f"{sum(c for c, _ in confirmed.values()):,} confirmed as the animal below, {rejected:,} rejected "
             "(not an animal). They are listed here separately and not added to the counts above.", "",
             "| Animal | Confirmed | Last confirmed |", "|---|---:|---|"]
    for name, (count, last) in sorted(confirmed.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"| {name} | {count} | {last} |")
    return lines


def render(days, by_hour_of_day, validation, confirmed, rejected, conditions, encounters):
    if not days:
        return "_No results yet. The tracker publishes here once a day after it starts running._"
    cats = categories()
    first, last = min(days), max(days)
    analyzed = sum(d["analyzed"] for d in days.values())
    daylight = sum(d["analyzed"] - d["dark"] - d["murky"] for d in days.values())  # clear-water daylight
    murky = sum(d["murky"] for d in days.values())

    totals = defaultdict(lambda: {"days": 0, "seen": 0, "max": 0, "first": None, "last": None, "encounters": 0})
    for d in sorted(days):
        for name, (seen, most) in days[d]["species"].items():
            t = totals[name]
            t["encounters"] += encounters.get((d, name), 0)
            t["days"] += 1
            t["seen"] += seen
            t["max"] = max(t["max"], most)
            t["first"] = t["first"] or d
            t["last"] = d

    lines = [
        f"_Last updated {datetime.now():%Y-%m-%d %H:%M} (Pacific). Tracking since {first}._" + tracker_status(),
        "",
        "| | |",
        "|---|---|",
        f"| Days tracked | {len(days)} |",
        f"| Snapshots analyzed (one every {SNAPSHOT_SECONDS} s while streaming) | {analyzed:,} |",
        f"| Clear-water daylight footage analyzed | {daylight * SNAPSHOT_SECONDS / 3600:,.1f} h |",
        f"| Daylight too murky to identify anything | {murky * SNAPSHOT_SECONDS / 3600:,.1f} h |",
        f"| Animal types seen | {sum(1 for n in totals if n != 'fish (unidentified)')} |",
        "",
        "### Animals seen",
        "",
        "The tracker can't tell individual fish apart, so none of these numbers count individuals:",
        "- **Encounters**: sightings of the same animal less than 30 minutes apart are one encounter (the",
        "  usual camera-trap rule for independent detections). A kelp bass that hangs around the camera for",
        "  an hour is one encounter. Two encounters can still be the same fish coming back.",
        "- **Snapshots**: how many snapshots (one every 10 s) it was in, i.e. how long it was around.",
        "- **Most at once (MaxN)**: the most seen in a single snapshot, the standard count for underwater",
        "  video because no fish can be counted twice. It undercounts big schools.",
        "",
        "Five or more of one kind in a snapshot is logged as a **school**; for schools of small fish",
        "(too small to name) the count is a rough estimate from the moving specks, rounded.",
        "",
        "Names are guesses by an AI model that wasn't trained on this camera. **Checked** says how many",
        "of its names a person has looked at so far, and how many were right.",
        "",
        "| Animal | Type | Encounters | Snapshots | % of clear-water snapshots | Most at once (MaxN) | Days seen | First seen | Last seen | Checked |",
        "|---|---|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for name, t in sorted(totals.items(), key=lambda kv: -kv[1]["seen"]):
        pct = 100 * t["seen"] / daylight if daylight else 0
        base = base_name(name)
        r, w = validation.get(base, (0, 0))
        checked = "—" if base in ("fish (unidentified)", "small fish") else f"{r - w} of {r} right" if r else "not yet"
        enc = "—" if name in NO_ENCOUNTERS else f"{t['encounters']:,}"
        lines.append(f"| {name} | {cats.get(base, '')} | {enc} | {t['seen']:,} | {pct:.2f}% | {t['max']} | "
                     f"{t['days']} | {t['first']} | {t['last']} | {checked} |")

    recent = [(date.fromisoformat(last) - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    recent = [d for d in recent if d >= first]  # nothing to chart before tracking began
    per_day = [sum(s for s, _ in days[d]["species"].values()) if d in days else 0 for d in recent]
    lines += [
        "",
        "### Sightings per day (up to the last 14 days)",
        "",
        "```mermaid",
        "xychart-beta",
        '    x-axis [' + ", ".join(f'"{d[5:]}"' for d in recent) + "]",
        '    y-axis "Animal snapshots"',
        "    bar [" + ", ".join(str(v) for v in per_day) + "]",
        "```",
        "",
        "### When animals show up (all days, Pacific time)",
        "",
        "```mermaid",
        "xychart-beta",
        '    x-axis "Hour of day" [' + ", ".join(str(h) for h in range(24)) + "]",
        '    y-axis "Animal snapshots"',
        "    bar [" + ", ".join(str(v) for v in by_hour_of_day) + "]",
        "```",
        "",
        "Daily numbers: [`results/daily_summary.csv`](results/daily_summary.csv). Learning from the data: "
        "[what brings animals in](results/conditions_model.md) (fitted once there are 3 weeks of data) and the "
        "[camera-trained classifier](results/camera_classifier.md) (trained from the review answers).",
    ]
    return "\n".join(lines + render_conditions(conditions) + render_confirmed(confirmed, rejected)
                     + render_validation(validation))


def update_readme(section):
    text = README.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + ".*?" + re.escape(END), re.S)
    README.write_text(pattern.sub(lambda _: f"{START}\n{section}\n{END}", text), encoding="utf-8")


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, creationflags=NO_WINDOW)


def publish_hourly(first_day):
    """Copies the hourly sightings and hourly conditions (from the first tracked day) into results/."""
    RESULTS.mkdir(exist_ok=True)
    if HOURLY_CSV.exists():
        shutil.copyfile(HOURLY_CSV, RESULTS / "hourly_summary.csv")
    if ENV_CSV.exists() and first_day:
        with ENV_CSV.open(encoding="utf-8") as src, (RESULTS / "environment_hourly.csv").open("w", newline="", encoding="utf-8") as dst:
            reader = csv.DictReader(src)
            writer = csv.DictWriter(dst, fieldnames=reader.fieldnames)
            writer.writeheader()
            writer.writerows(r for r in reader if r["date"] >= first_day)


def main(push=True, update_environment=True):
    effort, species = load_hourly()
    days, by_hour = build(effort, species)
    first_day = min(days) if days else None
    if update_environment:
        try:
            environment.update(date.fromisoformat(first_day) if first_day else None)
        except Exception as e:  # conditions are a bonus; never block the daily publish on them
            print(f"could not update conditions: {e}")
    publish_hourly(first_day)
    if CLASSIFIER_REPORT.exists():
        shutil.copyfile(CLASSIFIER_REPORT, RESULTS / "camera_classifier.md")
    encounters = load_encounters(effort)
    write_daily(days, encounters)
    validation = update_validation()
    confirmed, rejected, checks = read_reviews()
    for name, (checked, wrong) in checks.items():  # review-window checks count as validation too
        validation[name][0] += checked
        validation[name][1] += wrong
    conditions = daily_conditions(days, load_environment())
    write_daily_conditions(conditions)
    update_readme(render(days, by_hour, validation, confirmed, rejected, conditions, encounters))
    if not push:
        return
    git("add", "README.md", "results")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("nothing new to publish")
        return
    commit = git("commit", "-m", f"Update tracking results ({date.today().isoformat()})")
    pushed = git("push")
    print(commit.stdout.strip(), pushed.stderr.strip(), sep="\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main(push="--no-push" not in sys.argv)
