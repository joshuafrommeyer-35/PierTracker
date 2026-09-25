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
RESULTS = ROOT / "results"
DAILY_CSV = RESULTS / "daily_summary.csv"
VALIDATION_CSV = RESULTS / "validation.csv"
CROPS = ROOT / "data" / "crops"
DECISIONS_CSV = ROOT / "data" / "review" / "decisions.csv"
CONFIRMED_CSV = RESULTS / "confirmed_by_hand.csv"
ENV_CSV = environment.ENV_CSV
README = ROOT / "README.md"
SPECIES = ROOT / "tracker" / "species.json"
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
SNAPSHOT_SECONDS = 10  # LiveCams saves a frame this often (captureEverySeconds)
NO_WINDOW = 0x08000000


def load_hourly():
    effort = {}                                    # (date, hour) -> (analyzed, dark)
    species = defaultdict(lambda: [0, 0])          # (date, hour, name) -> [snapshots seen, max count]
    if not HOURLY_CSV.exists():
        return effort, species
    with HOURLY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["date"], int(row["hour"]))
            effort[key] = (int(row["snapshots_analyzed"]), int(row["snapshots_dark"]))
            if row["common_name"]:
                s = species[key + (row["common_name"],)]
                s[0] += int(row["snapshots_seen"])
                s[1] = max(s[1], int(row["max_count"]))
    return effort, species


def categories():
    spec = json.loads(SPECIES.read_text(encoding="utf-8"))
    cats = {s["common"]: s["category"] for s in spec["species"]}
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
    days = defaultdict(lambda: {"analyzed": 0, "dark": 0, "species": defaultdict(lambda: [0, 0])})
    for (d, _), (analyzed, dark) in effort.items():
        days[d]["analyzed"] += analyzed
        days[d]["dark"] += dark
    by_hour_of_day = [0] * 24
    for (d, h, name), (seen, most) in species.items():
        s = days[d]["species"][name]
        s[0] += seen
        s[1] = max(s[1], most)
        by_hour_of_day[h] += seen
    return days, by_hour_of_day


def write_daily(days):
    RESULTS.mkdir(exist_ok=True)
    with DAILY_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "snapshots_analyzed", "snapshots_dark", "common_name", "snapshots_seen", "max_count"])
        for d in sorted(days):
            day = days[d]
            rows = sorted(day["species"].items()) or [("", [0, 0])]
            for name, (seen, most) in rows:
                w.writerow([d, day["analyzed"], day["dark"], name, seen, most])


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


def render_conditions(days, env):
    """Daily conditions at the pier next to the day's sightings, last 7 tracked days."""
    recent = sorted(days)[-7:]
    if not env or not recent:
        return []

    def daily(d, column, how, hours=range(24)):
        vals = [float(env[(d, h)][column]) for h in hours if env.get((d, h), {}).get(column) not in (None, "")]
        return how(vals) if vals else None

    def fmt(v, digits=1):
        return "" if v is None else f"{v:.{digits}f}"

    lines = ["", "### Conditions at the pier", "",
             "From sensors on the pier: water temperature, turbidity and chlorophyll from the SCCOOS shore station "
             "(~5 m deep, next to the camera; only readings that passed quality control), and the tide from NOAA's "
             "La Jolla gauge. Hourly values for every day are in "
             "[`results/environment_hourly.csv`](results/environment_hourly.csv), next to the hourly sightings in "
             "[`results/hourly_summary.csv`](results/hourly_summary.csv).", "",
             "| Date | Water temp (°C) | Turbidity, daytime (NTU) | Chlorophyll (µg/L) | Tide range (m) | Animal snapshots |",
             "|---|---:|---:|---:|---:|---:|"]
    daytime = range(7, 19)
    for d in recent:
        lo = daily(d, "tide_predicted_m", min)
        hi = daily(d, "tide_predicted_m", max)
        sightings = sum(s for s, _ in days[d]["species"].values())
        lines.append(f"| {d} | {fmt(daily(d, 'pier_water_temp_c', statistics.mean))} | "
                     f"{fmt(daily(d, 'turbidity_ntu', statistics.median, daytime), 2)} | "
                     f"{fmt(daily(d, 'chlorophyll_ug_l', statistics.mean), 2)} | "
                     f"{fmt(hi - lo if lo is not None and hi is not None else None, 2)} | {sightings:,} |")
    return lines


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


def render(days, by_hour_of_day, validation, confirmed, rejected, env):
    if not days:
        return "_No results yet. The tracker publishes here once a day after it starts running._"
    cats = categories()
    first, last = min(days), max(days)
    analyzed = sum(d["analyzed"] for d in days.values())
    daylight = sum(d["analyzed"] - d["dark"] for d in days.values())

    totals = defaultdict(lambda: {"days": 0, "seen": 0, "max": 0, "first": None, "last": None})
    for d in sorted(days):
        for name, (seen, most) in days[d]["species"].items():
            t = totals[name]
            t["days"] += 1
            t["seen"] += seen
            t["max"] = max(t["max"], most)
            t["first"] = t["first"] or d
            t["last"] = d

    lines = [
        f"_Last updated {datetime.now():%Y-%m-%d %H:%M} (Pacific). Tracking since {first}._",
        "",
        "| | |",
        "|---|---|",
        f"| Days tracked | {len(days)} |",
        f"| Snapshots analyzed (one every {SNAPSHOT_SECONDS} s while streaming) | {analyzed:,} |",
        f"| Daylight footage analyzed | {daylight * SNAPSHOT_SECONDS / 3600:,.1f} h |",
        f"| Animal types seen | {sum(1 for n in totals if n != 'fish (unidentified)')} |",
        "",
        "### Animals seen",
        "",
        "\"Snapshots\" counts snapshots the animal was in, not individual animals: a fish that hangs around",
        "for a minute shows up in about six snapshots. \"Most at once\" is the biggest count in one snapshot.",
        "Five or more of one kind in a snapshot is logged as a **school**; for schools of small fish",
        "(too small to name) the count is a rough estimate from the moving specks, rounded.",
        "",
        "Names are guesses by an AI model that wasn't trained on this camera. **Checked** says how many",
        "of its names a person has looked at so far, and how many were right.",
        "",
        "| Animal | Type | Snapshots | % of daylight snapshots | Most at once | Days seen | First seen | Last seen | Checked |",
        "|---|---|---:|---:|---:|---:|---|---|---|",
    ]
    for name, t in sorted(totals.items(), key=lambda kv: -kv[1]["seen"]):
        pct = 100 * t["seen"] / daylight if daylight else 0
        base = base_name(name)
        r, w = validation.get(base, (0, 0))
        checked = "—" if base in ("fish (unidentified)", "small fish") else f"{r - w} of {r} right" if r else "not yet"
        lines.append(f"| {name} | {cats.get(base, '')} | {t['seen']:,} | {pct:.2f}% | {t['max']} | "
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
        "Daily numbers: [`results/daily_summary.csv`](results/daily_summary.csv).",
    ]
    return "\n".join(lines + render_conditions(days, env) + render_confirmed(confirmed, rejected)
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


def main(push=True):
    effort, species = load_hourly()
    days, by_hour = build(effort, species)
    first_day = min(days) if days else None
    try:
        environment.update(date.fromisoformat(first_day) if first_day else None)
    except Exception as e:  # conditions are a bonus; never block the daily publish on them
        print(f"could not update conditions: {e}")
    publish_hourly(first_day)
    write_daily(days)
    validation = update_validation()
    confirmed, rejected, checks = read_reviews()
    for name, (checked, wrong) in checks.items():  # review-window checks count as validation too
        validation[name][0] += checked
        validation[name][1] += wrong
    update_readme(render(days, by_hour, validation, confirmed, rejected, load_environment()))
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
