# Learning SQL with the pier data

The tracker keeps everything it sees in a SQLite database, `data/piertracker.db`. SQLite is a real SQL
database that lives in one file, with no server to run. The SQL you learn here works almost unchanged in
PostgreSQL, MySQL, BigQuery and the rest.

You'll work in a **sandbox**, a copy of the database you can break freely. `.reset` gives you a fresh
copy at any time. The tracker's own data is never touched.

## Getting started

```powershell
cd C:\Users\joshu\Projects\Wallpapers\LiveCams
tracker\.venv\Scripts\python tracker\sql.py
```

You get a `sql>` prompt. Type SQL ending with `;` (it can span several lines). Shell commands start with a
dot:
- `.tables` lists the tables
- `.schema sightings` shows a table's columns
- `.reset` gives you a fresh sandbox
- `.quit` leaves

For a point-and-click view, [DB Browser for SQLite](https://sqlitebrowser.org/) (free) can open
`sandbox/piertracker_sandbox.db`.

## The tables

| Table | One row per | Key columns |
|---|---|---|
| `snapshots` | frame the tracker analyzed (~every 10 s while the cam streams) | `taken_at`, `date`, `hour`, `dark`, `murky`, `visibility` |
| `sightings` | animal type in a snapshot | `taken_at`, `common_name`, `is_school`, `count`, `confidence`, `method`, `corrected_name` (from a review answer; `''` = not an animal) |
| `species` | animal the tracker knows | `common_name`, `scientific_name`, `category`, `look_alike_group` |
| `conditions` | hour at the pier | `date`, `hour`, `pier_water_temp_c`, `pier_temp_anomaly_c`, `turbidity_ntu`, `tide_predicted_m`, `tide_trend`, `oni` |
| `reviews` | answer in the review window | `kind`, `logged_as`, `decision`, `answer`, `reviewer` |
| `visits` | fish followed across frames (arrival to leaving) | `started_at`, `ended_at`, `looks`, `common_name`, `confidence` |

`sightings.taken_at` points at `snapshots.taken_at`, and `snapshots.date` + `snapshots.hour` point at
`conditions`. Those links are what JOINs use.

---

## Lesson 1: look around

```sql
SELECT * FROM sightings LIMIT 5;
```

`SELECT` picks columns (`*` = all), `FROM` picks the table, and `LIMIT` keeps the output short. Now name
the columns you want:

```sql
SELECT taken_at, common_name, count FROM sightings LIMIT 10;
```

**Try:** show the first 5 rows of `conditions`, but only the date, hour and water temperature.

<details><summary>Answer</summary>

```sql
SELECT date, hour, pier_water_temp_c FROM conditions LIMIT 5;
```
</details>

## Lesson 2: filter and sort

`WHERE` keeps only the rows you want, and `ORDER BY` sorts them (`DESC` = biggest first).

```sql
SELECT taken_at, count, confidence
FROM sightings
WHERE common_name = 'kelp bass'
ORDER BY confidence DESC;
```

Other conditions you can use:
- combine with `AND` / `OR`: `WHERE count >= 5 AND method = 'zero-shot'`
- match a list with `IN`: `WHERE common_name IN ('blacksmith', 'senorita')`
- match text patterns with `LIKE`: `WHERE common_name LIKE '%perch%'` (`%` = anything)

**Try:**
1. The 10 biggest schools ever seen: `is_school = 1`, sorted by `count`.
2. Every sighting after 3 PM on one day. Hint: `taken_at` is text like `2026-09-25T15:02:11`, and text
   compares in order, so `taken_at >= '2026-09-25T15'` works.

<details><summary>Answers</summary>

```sql
SELECT taken_at, common_name, count FROM sightings WHERE is_school = 1 ORDER BY count DESC LIMIT 10;

SELECT * FROM sightings WHERE taken_at >= '2026-09-25T15' AND taken_at < '2026-09-26';
```
</details>

## Lesson 3: count and group

Aggregate functions (`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`) squash many rows into one. `GROUP BY` does it
once per group.

```sql
SELECT common_name, COUNT(*) AS snapshots_seen, MAX(count) AS most_at_once
FROM sightings
GROUP BY common_name
ORDER BY snapshots_seen DESC;
```

`HAVING` filters *groups*, the way `WHERE` filters rows: add `HAVING COUNT(*) >= 10` to hide the rare
ones.

**Try:**
1. How many snapshots were dark, how many murky, and how many clear? Hint: `SUM(dark)`, `SUM(murky)` over
   `snapshots`.
2. Which hour of the day has the most sightings? Hint: sightings don't have an hour column yet, but
   `substr(taken_at, 12, 2)` pulls it out of the timestamp.

<details><summary>Answers</summary>

```sql
SELECT COUNT(*) AS total, SUM(dark) AS dark, SUM(murky) AS murky,
       COUNT(*) - SUM(dark) - SUM(murky) AS clear
FROM snapshots;

SELECT substr(taken_at, 12, 2) AS hour, COUNT(*) AS sightings
FROM sightings GROUP BY hour ORDER BY sightings DESC;
```
</details>

## Lesson 4: join tables

A `JOIN` lines up rows from two tables that share a value. This puts each sighting next to its
snapshot's date and hour:

```sql
SELECT g.common_name, g.count, p.date, p.hour
FROM sightings AS g
JOIN snapshots AS p ON p.taken_at = g.taken_at
LIMIT 10;
```

(`AS g` / `AS p` are short nicknames.) Add a second join to bring in that hour's conditions:

```sql
SELECT g.common_name, g.count, c.pier_temp_anomaly_c, c.turbidity_ntu, c.tide_trend
FROM sightings AS g
JOIN snapshots AS p ON p.taken_at = g.taken_at
JOIN conditions AS c ON c.date = p.date AND c.hour = p.hour
LIMIT 10;
```

`LEFT JOIN` keeps rows even when there's no match. Use it when an hour might be missing conditions (the
row stays, with `NULL`s).

**Try:** for each animal, the average water temperature anomaly when it was seen. Is anything seen
mainly when it's warmer than normal?

<details><summary>Answer</summary>

```sql
SELECT g.common_name, COUNT(*) AS n, ROUND(AVG(c.pier_temp_anomaly_c), 2) AS avg_anomaly
FROM sightings g
JOIN snapshots p ON p.taken_at = g.taken_at
JOIN conditions c ON c.date = p.date AND c.hour = p.hour
GROUP BY g.common_name HAVING n >= 10 ORDER BY avg_anomaly DESC;
```
</details>

## Lesson 5: rates, not counts (the one that matters for science)

"More sightings in the afternoon" might just mean the camera was running more in the afternoon, or the
morning was murky. To compare fairly, divide by **effort**: the clear-water snapshots in that hour. This
is the step people most often skip.

`WITH` names an intermediate result (a CTE), so a long query reads in steps:

```sql
WITH effort AS (
    SELECT hour, COUNT(*) AS clear_snapshots
    FROM snapshots WHERE dark = 0 AND murky = 0
    GROUP BY hour
),
schools AS (
    SELECT p.hour, COUNT(*) AS with_school
    FROM sightings g JOIN snapshots p ON p.taken_at = g.taken_at
    WHERE g.common_name = 'small fish' AND g.is_school = 1
    GROUP BY p.hour
)
SELECT e.hour, e.clear_snapshots, COALESCE(s.with_school, 0) AS with_school,
       ROUND(100.0 * COALESCE(s.with_school, 0) / e.clear_snapshots, 1) AS pct_of_snapshots
FROM effort e LEFT JOIN schools s ON s.hour = e.hour
ORDER BY e.hour;
```

`COALESCE(x, 0)` turns a `NULL` (an hour with no schools) into 0. `100.0` rather than `100` forces
decimal division.

**Try:** the same thing for any animal you pick, by day instead of hour.

## Lesson 6: save a query as a view

A view is a saved query that acts like a table. Make the join from Lesson 4 permanent:

```sql
CREATE VIEW sightings_with_conditions AS
SELECT g.*, p.date, p.hour, c.pier_temp_anomaly_c, c.turbidity_ntu, c.chlorophyll_ug_l,
       c.tide_predicted_m, c.tide_trend, c.oni
FROM sightings g
JOIN snapshots p ON p.taken_at = g.taken_at
LEFT JOIN conditions c ON c.date = p.date AND c.hour = p.hour;

SELECT common_name, tide_trend, COUNT(*) FROM sightings_with_conditions GROUP BY 1, 2;
```

(`GROUP BY 1, 2` = group by the 1st and 2nd selected columns.)

## Lesson 7: window functions

Window functions compute across rows *without* squashing them into one: running totals, ranks, "the
previous row".

```sql
-- first and last time each animal was seen
SELECT common_name, MIN(taken_at) AS first_seen, MAX(taken_at) AS last_seen FROM sightings GROUP BY 1;

-- sightings per day, with a running total
SELECT day, n, SUM(n) OVER (ORDER BY day) AS running_total
FROM (SELECT substr(taken_at, 1, 10) AS day, COUNT(*) AS n FROM sightings GROUP BY day);

-- each day's top animal
SELECT * FROM (
    SELECT substr(taken_at, 1, 10) AS day, common_name, COUNT(*) AS n,
           RANK() OVER (PARTITION BY substr(taken_at, 1, 10) ORDER BY COUNT(*) DESC) AS rnk
    FROM sightings GROUP BY day, common_name
) WHERE rnk = 1;
```

`LAG` looks at the previous row. The results table's **Encounters** column is built this way: a
sighting starts a new encounter only if the same animal wasn't seen in the previous 30 minutes, so one
kelp bass hanging around for an hour counts once.

```sql
WITH gaps AS (
    SELECT common_name, taken_at,
           (julianday(taken_at) - julianday(LAG(taken_at) OVER (PARTITION BY common_name ORDER BY taken_at)))
               * 24 * 60 AS minutes_since_last
    FROM sightings
)
SELECT common_name, COUNT(*) AS encounters
FROM gaps
WHERE minutes_since_last IS NULL OR minutes_since_last >= 30
GROUP BY common_name
ORDER BY encounters DESC;
```

**Try:** change 30 to 5 and to 60. Camera-trap studies found the rule gives stable results anywhere in
that range. Does it here?

## Lesson 8: your own question

Some to try once there are a few weeks of data:
- Do schools get bigger when the water is warmer than normal (the El Niño effect)?
- Is anything seen more on a rising tide than a falling one? (Remember Lesson 5: use rates.)
- How often is the camera too murky to use, and does that line up with the pier's `turbidity_ntu`
  sensor?
- In the `reviews` table, how often was the tracker's logged name right (`kind = 'check'`), per animal?
- From `visits`: how long does a kelp bass usually stay (`julianday(ended_at) - julianday(started_at)`,
  times 86400 for seconds)? Which animals come back most often in a day?
