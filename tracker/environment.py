"""Conditions at the pier, to put the sightings in context (and to model them later).

Hourly values, in this PC's local time like the rest of the tracker, go into
data/environment_hourly.csv from two public, quality-controlled sources on Scripps Pier:

  * NOAA CO-OPS tide gauge 9410230 "La Jolla": predicted tide and observed water level
    (meters above MLLW), water and air temperature, wind, air pressure.
  * SCCOOS Automated Shore Station, Scripps Pier (sensors ~5 m deep, close to the camera's
    ~4 m), served by the CeNCOOS ERDDAP: water temperature, salinity, dissolved oxygen, pH,
    turbidity and chlorophyll. Only readings that passed the station's QARTOD quality tests
    (flag 1) are used; the rest are dropped.

Each value is the median of that hour's readings. Stored days aren't fetched again, except
the last two (their data is still arriving). publish_results.py runs this once a day; it can
also be run by hand.
"""

import csv
import io
import json
import logging
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_CSV = ROOT / "data" / "environment_hourly.csv"
NOAA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
NOAA_STATION = "9410230"
ERDDAP_URL = "https://erddap.cencoos.org/erddap/tabledap/scripps-pier-automated-shore-sta-1.csv"
USER_AGENT = "PierTracker (https://github.com/joshuafrommeyer-35/PierTracker)"
REFETCH_DAYS = 2
CHUNK_DAYS = 30  # NOAA serves at most ~31 days of 6-minute data per request

# NOAA product -> request parameters, and column -> (product, value field)
NOAA_PRODUCTS = {
    "predictions": {"datum": "MLLW", "interval": "h"},
    "water_level": {"datum": "MLLW"},
    "water_temperature": {"interval": "h"},
    "air_temperature": {"interval": "h"},
    "wind": {"interval": "h"},
    "air_pressure": {"interval": "h"},
}
NOAA_COLUMNS = {
    "tide_predicted_m": ("predictions", "v"),
    "water_level_m": ("water_level", "v"),
    "noaa_water_temp_c": ("water_temperature", "v"),
    "air_temp_c": ("air_temperature", "v"),
    "wind_speed_ms": ("wind", "s"),
    "wind_gust_ms": ("wind", "g"),
    "wind_dir_deg": ("wind", "d"),
    "air_pressure_hpa": ("air_pressure", "v"),
}
# column -> ERDDAP variable; its "<variable>_qc_agg" flag must be 1 (passed)
PIER_COLUMNS = {
    "pier_water_temp_c": "sea_water_temperature_ctd",
    "salinity_psu": "sea_water_practical_salinity_ctd",
    "oxygen_mg_l": "mass_concentration_of_oxygen_in_sea_water_ctd",
    "ph": "sea_water_ph_reported_on_total_scale_seaphox_external",
    "turbidity_ntu": "sea_water_turbidity_eco",
    "chlorophyll_ug_l": "mass_concentration_of_chlorophyll_in_sea_water_eco",
}
COLUMNS = ["date", "hour", *NOAA_COLUMNS, "tide_trend", *PIER_COLUMNS]

log = logging.getLogger("environment")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def local_hour(utc: datetime):
    t = utc.astimezone()  # this PC's time zone, like the tracker's own timestamps
    return t.date().isoformat(), t.hour


def noaa(begin: date, end: date, readings):
    for product, params in NOAA_PRODUCTS.items():
        query = {"station": NOAA_STATION, "product": product, "begin_date": f"{begin:%Y%m%d}",
                 "end_date": f"{end:%Y%m%d}", "time_zone": "gmt", "units": "metric", "format": "json",
                 "application": "PierTracker", **params}
        try:
            body = json.loads(fetch(f"{NOAA_URL}?{urllib.parse.urlencode(query)}"))
        except (OSError, ValueError) as e:
            log.warning("NOAA %s: %s", product, e)
            continue
        if "error" in body:
            log.warning("NOAA %s: %s", product, body["error"].get("message"))
            continue
        for row in body.get("predictions") or body.get("data") or []:
            key = local_hour(datetime.strptime(row["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc))
            for column, (p, field) in NOAA_COLUMNS.items():
                if p == product and row.get(field) not in (None, ""):
                    readings[key][column].append(float(row[field]))


def pier(begin: date, end: date, readings):
    variables = ["time"] + [v + suffix for v in PIER_COLUMNS.values() for suffix in ("", "_qc_agg")]
    query = ",".join(variables) + f"&time>={begin.isoformat()}T00:00:00Z&time<{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
    try:
        text = fetch(f"{ERDDAP_URL}?{urllib.parse.quote(query, safe=',&=')}").decode("utf-8")
    except OSError as e:
        log.warning("SCCOOS pier station: %s", e)
        return
    rows = list(csv.DictReader(io.StringIO(text)))[1:]  # the first row holds units
    for row in rows:
        key = local_hour(datetime.fromisoformat(row["time"].replace("Z", "+00:00")))
        for column, variable in PIER_COLUMNS.items():
            if row.get(variable + "_qc_agg") == "1" and row.get(variable) not in (None, "", "NaN"):
                readings[key][column].append(float(row[variable]))


def load():
    if not ENV_CSV.exists():
        return {}
    with ENV_CSV.open(encoding="utf-8") as f:
        return {(r["date"], int(r["hour"])): r for r in csv.DictReader(f)}


def update(first_day: date = None) -> Path:
    """Fetches whatever is missing (from first_day, or yesterday on a first run) through today."""
    rows = load()
    today = date.today()
    if rows:
        start = date.fromisoformat(max(d for d, _ in rows)) - timedelta(days=REFETCH_DAYS - 1)
        if first_day and first_day < date.fromisoformat(min(d for d, _ in rows)):
            start = first_day
    else:
        start = first_day or today - timedelta(days=1)

    readings = defaultdict(lambda: defaultdict(list))
    begin = start
    while begin <= today:
        end = min(begin + timedelta(days=CHUNK_DAYS - 1), today)
        noaa(begin - timedelta(days=1), end, readings)  # UTC vs. local: a day of margin on each side
        pier(begin - timedelta(days=1), end + timedelta(days=1), readings)
        begin = end + timedelta(days=1)

    for (d, h), values in readings.items():
        if d < start.isoformat():
            continue
        row = rows.setdefault((d, h), {"date": d, "hour": h})
        for column, vals in values.items():
            row[column] = round(statistics.median(vals), 3)

    # Tide trend from the predictions: +1 rising, -1 falling over the next hour.
    for (d, h), row in rows.items():
        nxt = datetime.fromisoformat(d) + timedelta(hours=h + 1)
        after = rows.get((nxt.date().isoformat(), nxt.hour), {}).get("tide_predicted_m")
        if row.get("tide_predicted_m") not in (None, "") and after not in (None, ""):
            row["tide_trend"] = 1 if float(after) > float(row["tide_predicted_m"]) else -1

    ENV_CSV.parent.mkdir(parents=True, exist_ok=True)
    with ENV_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for key in sorted(rows, key=lambda k: (k[0], int(k[1]))):
            w.writerow(rows[key])
    return ENV_CSV


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(update())
