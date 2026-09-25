"""Conditions at the pier, to put the sightings in context (and to model them later).

Hourly values, in this PC's local time like the rest of the tracker, go into
data/environment_hourly.csv from two public, quality-controlled sources on Scripps Pier:

  * NOAA CO-OPS tide gauge 9410230 "La Jolla": predicted tide and observed water level
    (meters above MLLW), water and air temperature, wind, air pressure.
  * SCCOOS Automated Shore Station, Scripps Pier (sensors ~5 m deep, close to the camera's
    ~4 m), served by the CeNCOOS ERDDAP: water temperature, salinity, dissolved oxygen, pH,
    turbidity and chlorophyll. Only readings that passed the station's QARTOD quality tests
    (flag 1) are used; the rest are dropped.

Two columns put the season in context, which matters this year with a strong El Niño:

  * pier_temp_anomaly_c: the pier water temperature minus its normal for that date, from the
    same shore station's daily means since 2013 (smoothed over +-15 days; computed once a year).
  * oni: NOAA's Oceanic Nino Index (the official El Nino/La Nina measure; El Nino at +0.5 or
    more), the latest 3-month value available for that month.

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
CLIMATOLOGY_CSV = ROOT / "data" / "pier_temp_climatology.csv"
ONI_CSV = ROOT / "data" / "enso_oni.csv"
ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
CLIMATOLOGY_SMOOTH_DAYS = 15
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
COLUMNS = ["date", "hour", *NOAA_COLUMNS, "tide_trend", *PIER_COLUMNS, "pier_temp_anomaly_c", "oni"]
ONI_SEASON_CENTER = {"DJF": 1, "JFM": 2, "FMA": 3, "MAM": 4, "AMJ": 5, "MJJ": 6,
                     "JJA": 7, "JAS": 8, "ASO": 9, "SON": 10, "OND": 11, "NDJ": 12}

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


def climatology():
    """Normal pier water temperature for each day of the year: {day_of_year: degrees C}."""
    stale = not CLIMATOLOGY_CSV.exists() or (
        datetime.now().timestamp() - CLIMATOLOGY_CSV.stat().st_mtime > 365 * 86400)
    if stale:
        last_year = date.today().year - 1
        query = (f'time,{PIER_COLUMNS["pier_water_temp_c"]}&{PIER_COLUMNS["pier_water_temp_c"]}_qc_agg=1'
                 f'&time<{last_year + 1}-01-01T00:00:00Z&orderByMean("time/1day")')
        text = fetch(f"{ERDDAP_URL}?{urllib.parse.quote(query, safe=',&=')}").decode("utf-8")
        by_day = defaultdict(list)
        for row in list(csv.DictReader(io.StringIO(text)))[1:]:
            value = row[PIER_COLUMNS["pier_water_temp_c"]]
            if value not in ("", "NaN"):
                by_day[datetime.fromisoformat(row["time"].replace("Z", "+00:00")).timetuple().tm_yday].append(float(value))
        years = {datetime.fromisoformat(r["time"].replace("Z", "+00:00")).year
                 for r in list(csv.DictReader(io.StringIO(text)))[1:]}
        with CLIMATOLOGY_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["day_of_year", "normal_temp_c", "days_used", f"years {min(years)}-{max(years)}"])
            for doy in range(1, 367):
                window = [v for d in range(doy - CLIMATOLOGY_SMOOTH_DAYS, doy + CLIMATOLOGY_SMOOTH_DAYS + 1)
                          for v in by_day.get((d - 1) % 366 + 1, [])]
                w.writerow([doy, round(statistics.mean(window), 3) if window else "", len(window)])
    with CLIMATOLOGY_CSV.open(encoding="utf-8") as f:
        return {int(r["day_of_year"]): float(r["normal_temp_c"]) for r in csv.DictReader(f) if r["normal_temp_c"]}


def oni():
    """NOAA's Oceanic Nino Index: {(year, center month): anomaly}. Refreshed weekly."""
    if not ONI_CSV.exists() or datetime.now().timestamp() - ONI_CSV.stat().st_mtime > 7 * 86400:
        lines = fetch(ONI_URL).decode("utf-8").split("\n")[1:]
        with ONI_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["season", "year", "center_month", "oni"])
            for line in lines:
                parts = line.split()
                if len(parts) == 4 and parts[0] in ONI_SEASON_CENTER:
                    season, year, anomaly = parts[0], int(parts[1]), float(parts[3])
                    # NDJ 2025 is centered on December 2025; DJF 2026 on January 2026.
                    w.writerow([season, year, ONI_SEASON_CENTER[season], anomaly])
    with ONI_CSV.open(encoding="utf-8") as f:
        return {(int(r["year"]), int(r["center_month"])): float(r["oni"]) for r in csv.DictReader(f)}


def latest_oni(index, year, month):
    """The most recent ONI value at or before (year, month)."""
    past = [k for k in index if k <= (year, month)]
    return index[max(past)] if past else None


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

    # Season context: pier temperature vs. its normal for the date, and the El Nino index.
    try:
        normal = climatology()
    except (OSError, ValueError) as e:
        log.warning("temperature normals: %s", e)
        normal = {}
    try:
        enso = oni()
    except (OSError, ValueError) as e:
        log.warning("El Nino index: %s", e)
        enso = {}
    for (d, h), row in rows.items():
        day = date.fromisoformat(d)
        temp = row.get("pier_water_temp_c")
        doy = day.timetuple().tm_yday
        if temp not in (None, "") and doy in normal:
            row["pier_temp_anomaly_c"] = round(float(temp) - normal[doy], 2)
        if enso:
            value = latest_oni(enso, day.year, day.month)
            if value is not None:
                row["oni"] = value

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
