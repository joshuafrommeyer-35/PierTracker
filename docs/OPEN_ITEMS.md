# Open items

## Waiting on more data

These are built and switch themselves on; they only need time or answers.

| Item | What it needs | Where it stands | How to check |
|---|---|---|---|
| **Camera-trained classifier**: names fish the way they look on *this* camera, learned from review answers | 12 answers per animal (at least 3 kinds, "not an animal" counts as one); about 30 each to be reliable | 0 answers (2026-09-25) | Bottom line of the review window, or `tracker\.venv\Scripts\python tracker\ml\train_classifier.py --status`. Retrained every night; switches on only if it beats zero-shot by 5+ points |
| **What brings animals in**: seen-in-the-hour (yes/no) vs. water temperature anomaly, turbidity, chlorophyll, tide, time of day | 21 days of daylight data; an animal needs sightings in 30+ hours to get its own model | Day 1 (2026-09-25) | [`results/conditions_model.md`](../results/conditions_model.md), refitted every night |
| **El Niño comparison**: do visitors change as this El Niño peaks and fades? | Months of data spanning a real change in the ONI index (it's ~+1.8 now) | Collecting | The ONI column in `data/environment_hourly.csv`; the model adds ONI as a predictor by itself once it varies |
| **Occupancy models and forecasts**: "not there" vs. "there but too murky to see"; odds of a ray in the next hour | A season (~3 months) | Collecting | Planned (README, "Next steps") |
| **Detector trained on this camera**: better counts of small fish, fewer misses | Thousands of frames from this camera for self-supervised pretraining (Li et al. 2025 used 40,000) | Frame bank started 2026-09-25 (one frame per 20 min, one per 5 min while a named animal is in view; ~20 MB/day) | `data/frame_bank/` and the Google Drive backup. At this rate it takes months; raising the rate is a disk-space decision |

## In progress

- **Reference photos** (started 2026-09-25): does showing the model iNaturalist photos degraded to camera
  conditions beat matching species names alone? Needs no camera data. Result decides whether it's
  switched on.

## Needs a person

- **Review queue**: especially the "ocean whitefish" cards (likely juvenile blacksmith) and the cards of
  the hanging growth and the round growth on the piling ("not an animal"). Every answer now also teaches
  the fixture check (the gallery of confirmed fixtures and animals): "not an animal" on piling-edge
  cards stops them from coming back, and approving the lobster lets it be logged even while it sits
  still in its crevice.
- **SQL walkthrough** in the sandbox ([`SQL_WALKTHROUGH.md`](SQL_WALKTHROUGH.md)).

## To watch

- **First unattended nightly run** (night of 2026-09-25): results publish and the Google Drive backup.
  Check `logs/nightly.log`.
- **Fixed-thing filter**: `logs/tracker.log` lines end with "N fixed thing(s) ignored". Make sure real fish
  near the pilings still get logged (the resident kelp bass by the round growth did, 2026-09-25).
