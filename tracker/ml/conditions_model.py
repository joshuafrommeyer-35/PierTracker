"""What brings animals in? Relates hourly sightings to the conditions at the pier.

For each animal with enough sightings, this fits a logistic regression of whether the animal was
seen at all in each clear daylight hour (yes/no), against the conditions below. Yes/no per hour,
not snapshot counts: the tracker can't tell individuals apart, and one kelp bass hanging around
the camera for an hour would otherwise look like hundreds of sightings. How much clear footage the
hour had (effort) is a predictor too, since more looking finds more.

The conditions:

  - how much warmer or colder than normal the water is (pier temperature anomaly; the El Nino
    signal at the pier),
  - turbidity (water clarity) and chlorophyll (plankton),
  - tide height and whether it's rising or falling,
  - time of day,
  - the El Nino index (ONI) itself, once the data spans months in which it changes.

Predictors are standardized, so each result reads "per typical (1 SD) change". Output:
results/conditions_model.md with rate ratios and 95% intervals, or how much data is still
needed. Run by nightly.py once a day, or by hand:
    tracker\\.venv\\Scripts\\python tracker\\ml\\conditions_model.py

Caveats: it shows associations, not causes, and hours aren't fully independent (a fish that
lingers shows up in neighbouring hours), so the intervals are optimistic. Occupancy models and
GAMs are the natural next steps once there's a season of data.
"""

import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

LIVECAMS = Path(__file__).resolve().parent.parent.parent
CONDITIONS = LIVECAMS / "data" / "environment_hourly.csv"
REPORT = LIVECAMS / "results" / "conditions_model.md"
MIN_DAYS = 21            # days of daylight data before any model is fitted
MIN_HOURS_SEEN = 30      # hours an animal must have been seen in to get its own model
PREDICTORS = {
    "pier_temp_anomaly_c": "water warmer than normal",
    "turbidity_ntu": "turbidity",
    "chlorophyll_ug_l": "chlorophyll",
    "tide_predicted_m": "tide height",
    "tide_trend": "tide rising",
    "hour_sin": "time of day (sin)",
    "hour_cos": "time of day (cos)",
    "oni": "El Nino index",
}


def hourly_rows():
    """The hourly summary from the database (db.hourly)."""
    if not db.DB_PATH.exists():
        return []
    with closing(db.connect()) as con:
        return db.hourly(con)


def load(rows=None):
    sightings = pd.DataFrame(hourly_rows() if rows is None else rows)
    conditions = pd.read_csv(CONDITIONS)
    if "snapshots_murky" not in sightings:
        sightings["snapshots_murky"] = 0
    sightings["snapshots_murky"] = sightings["snapshots_murky"].fillna(0)
    # Effort = clear-water daylight snapshots: dark and murky ones couldn't have seen anything.
    effort = (sightings.groupby(["date", "hour"])[["snapshots_analyzed", "snapshots_dark", "snapshots_murky"]].first()
              .assign(daylight=lambda d: d.snapshots_analyzed - d.snapshots_dark - d.snapshots_murky)
              .query("daylight > 0").reset_index())
    seen = sightings.dropna(subset=["common_name"]).pivot_table(
        index=["date", "hour"], columns="common_name", values="snapshots_seen", aggfunc="sum", fill_value=0)
    table = effort.merge(seen.reset_index(), on=["date", "hour"], how="left").fillna(0)
    table = table.merge(conditions, on=["date", "hour"], how="left")
    table["hour_sin"] = np.sin(2 * np.pi * table.hour / 24)
    table["hour_cos"] = np.cos(2 * np.pi * table.hour / 24)
    return table, [c for c in seen.columns]


def fit(table, animal):
    import statsmodels.api as sm

    usable = [p for p in PREDICTORS if p in table and table[p].notna().mean() > 0.8 and table[p].std() > 0]
    if "oni" in usable and table["oni"].max() - table["oni"].min() < 0.3:
        usable.remove("oni")  # the index barely changed over this data: nothing to learn from it yet
    data = table.dropna(subset=usable)
    X = (data[usable] - data[usable].mean()) / data[usable].std()
    seen = (data[animal] > 0).astype(int)
    X = sm.add_constant(X.assign(log_effort=np.log(data["daylight"])))
    model = sm.Logit(seen, X).fit(disp=0, maxiter=200)
    ci = model.conf_int()
    rows = []
    for p in usable:
        rows.append((PREDICTORS[p], np.exp(model.params[p]), np.exp(ci.loc[p, 0]), np.exp(ci.loc[p, 1]), model.pvalues[p]))
    return len(data), sorted(rows, key=lambda r: r[4])


def main():
    lines = [f"# What brings animals in? ({datetime.now():%Y-%m-%d})", ""]
    rows = hourly_rows()
    if not rows or not CONDITIONS.exists():
        lines.append("No data yet.")
    else:
        table, animals = load(rows)
        days = table.date.nunique()
        if days < MIN_DAYS:
            lines.append(f"Waiting for data: {days} of the {MIN_DAYS} days needed before fitting models.")
        else:
            lines += ["Logistic regressions of whether the animal was seen in a clear daylight hour (yes/no), so "
                      "a fish that lingers counts once per hour, not once per snapshot. An **odds ratio** of 1.5 "
                      "means the odds of seeing it in an hour are 1.5x higher per typical (1 SD) increase in "
                      "that condition; below 1, lower. Associations, not causes; intervals are optimistic "
                      "because neighbouring hours aren't independent.", ""]
            for animal in animals:
                hours_seen = int((table[animal] > 0).sum())
                if hours_seen < MIN_HOURS_SEEN:
                    continue
                try:
                    n, rows = fit(table, animal)
                except Exception as e:  # a model that won't converge shouldn't stop the others
                    lines += [f"## {animal}", "", f"_Model didn't fit: {e}_", ""]
                    continue
                lines += [f"## {animal}", "", f"{hours_seen} hours with sightings, {n} daylight hours modeled.", "",
                          "| Condition | Odds ratio | 95% interval | p |", "|---|---:|---|---:|"]
                lines += [f"| {name} | {rr:.2f} | {lo:.2f}-{hi:.2f} | {p:.3f} |" for name, rr, lo, hi, p in rows]
                lines.append("")
            waiting = [a for a in animals if int((table[a] > 0).sum()) < MIN_HOURS_SEEN]
            if waiting:
                lines += [f"Not modeled yet (fewer than {MIN_HOURS_SEEN} hours with sightings): " + ", ".join(waiting)]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
