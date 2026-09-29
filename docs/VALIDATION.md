# Validation

Back to the [README](../README.md). How the tracker works: [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

The models were not trained on this camera, so the tracker's work gets checked two ways.

## 1. Pre-deployment check on reference images

Before going live, the whole pipeline ran on 11 images: clear photos of local species from Wikipedia, and
frames from this cam (a news frame and the cam's own thumbnails). The first run exposed two problems. The
big-animal check reported "octopus" on ordinary fish photos, and some partial crops got confident wrong
names. The thresholds were then raised and the check now has to beat every label. Results after that fix:

| Image | Expected | Tracker output | |
|---|---|---|---|
| Garibaldi | garibaldi | garibaldi (1.00) | ✅ |
| California sheephead | sheephead | California sheephead (1.00) | ✅ |
| Opaleye | opaleye | opaleye (0.98) | ✅ |
| California sea lion, underwater | sea lion | California sea lion (1.00) | ✅ |
| California spiny lobster | lobster | California spiny lobster (1.00) | ✅ |
| Spiny lobster crawling on **this cam's** lens (news frame) | lobster | California spiny lobster (0.73) | ✅ |
| Bat ray | bat ray | bat ray (1.00) + 1 fish (unidentified): a second box on the same ray | ⚠️ |
| Leopard shark with two dark fish, in kelp | leopard shark + 2 fish | leopard shark (0.87), blacksmith (0.83), and 1 fish sent to review (halfmoon 45% / blacksmith 42%) | ⚠️ the fish look like blacksmith or halfmoon; the photo can't settle it |
| Typical daytime view from **this cam** (tiny, distant fish in green water) | a few specks | nothing: the fish are ~10 px, too small to detect | ⚠️ missed |
| Above-water photo of the pier | nothing | nothing | ✅ |
| **This cam** at night | nothing | skipped as dark | ✅ |

The OpenVINO detector was also checked against the original RF-DETR model on the same images. It found
the same boxes with the same confidences (within 0.01).

The tile scan was tested on the same images and added no false sightings.

**Takeaways:** fish that are clear and close are named reliably. Look-alike species (blacksmith, halfmoon,
opaleye) come out as close calls, and those go to the review queue instead of into the stats. Tiny,
distant fish in murky water are missed, which is why the stats count presence rather than claiming a
census.

One honest correction from testing: that shark photo was first described here as "three leopard sharks",
and "blacksmith" was counted as a wrong name. The review picture showed two dark fish next to one shark.
Reviewing the tracker's pictures catches mistakes in both directions.

## 2. First live morning (2026-09-25)

The first version ran on the live cam from sunrise to 08:40 and got a lot wrong. Here's what a look through
its 84 sample crops and 208 review pictures showed:

| Problem | Example | Fix |
|---|---|---|
| Pilings and the hanging rope boxed as fish and given fish names | the rope logged as "sargo" (0.79), a piling as "salema" | Motion filter: things that don't move are ignored |
| Tiles of the piling named as crabs | "sheep crab" at up to 0.89 on bare piling | Tile scan replaced by moving areas only |
| Small fish given non-fish names | 40–60 px fish logged as "octopus", "jellyfish", "bat ray" | Fish-detector boxes get fish names only |
| Tiny backlit fish given confident species names | 50 px silhouettes as "blacksmith" (0.9+) | Under 80 px: "fish (unidentified)" |
| A night frame got through | noise with a bright blob at 04:40 | Night check also looks at color |
| Review queue flooded | 208 pictures in ~2 hours | At most ~10 an hour, only things a person can judge |
| A school of hundreds logged as individual fish | "11 fish (unidentified)" every 10 s | One "small fish (school)" sighting per snapshot, with a rough size |

The fixed version was replayed on 29 consecutive live frames (5 minutes) and then deployed. The data
logged before the fix was set aside and isn't in the results.

On those 5 minutes the fixed version logged:

| Result | Verdict |
|---|---|
| 136 fish (unidentified) | ✅ the school, honestly unnamed |
| 1 **kelp bass** (0.99): a big blotchy bass cruising past a piling | ✅ looks right |
| 2 Pacific sardine | ✅ plausible: slender, silvery |
| A "spiny lobster" at a piling edge (0.72) | Held back and sent to review. I first judged it "fish passing a cable". But the long curved "cable" and the lumpy shape at the piling were **gone an hour later**, so it was almost certainly a real lobster, with its long antennae. The model saw it; the quick human look didn't. |
| 23 **blacksmith** (0.94–0.99) | ❓ doubtful. Several crops look like yellow **señoritas** or olive fish, not dark blacksmith. BioCLIP is overconfident on green, blurry footage. |

## 3. Fixing the overconfidence: a controlled test

Live footage has no answer key, so the next test used one. 150 research-grade iNaturalist photos of
25 local species were made to look like this camera:
- the fish shrunk to 80–130 px
- the water's color cast applied: red nearly gone, measured from real frames as green in the morning
  and blue at midday
- JPEG compression and noise added

That gave 300 test images, and each method was scored the way the tracker uses it.

| Method | Top guess right | Of names logged at ≥0.6, right | Of names logged at ≥0.9, right |
|---|---:|---:|---:|
| BioCLIP 2 (the first version) | 46% | 57% | 67% (22% of images named) |
| BioCLIP 2 + color correction | 22% | 28% | 49% |
| **BioCLIP 2.5** | **53%** | **64%** | **79% (49% named)** |
| BioCLIP 2.5 + color correction | 39% | 49% | 69% |

- **Color correction makes it much worse.** The model knows blue-green underwater photos, and "fixing"
  the colors distorts what it relies on. It isn't used.
- **BioCLIP 2.5 is better at every cutoff,** so the tracker switched to it. It runs on the iGPU,
  using ~0.65 GB more memory.
- **The cutoffs were then tuned on the deployed model** (OpenVINO, iGPU). With species at 0.85 and
  look-alike groups at 0.90, **81% of logged names were right**, with 56% of fish named. The rest are
  logged as unidentified rather than guessed. The first version's 0.6 cutoff gave ~57–64%.
- **Other models tested and not used:**
  - iNaturalist-trained classifiers (EVA-02, ConvNeXt on iNat 2021) know only 10 of 30 key local
    species: no blacksmith, señorita, topsmelt, sardine or anchovy. A trained classifier can't name what
    it wasn't trained on, and their license is non-commercial.
  - Orange's [marine-detect](https://github.com/Orange-OpenSource/marine-detect) MegaFauna model
    (sharks, rays, turtles) found 0 of 48 sharks and rays pasted into real pier frames. On clean photos
    it called leopard sharks "turtle". It was trained on tropical reef species.

**Murky-water calibration.** Sixty known fish were pasted into real pier frames, and haze + blur was
increased step by step:

| Murk | Visibility score | Fish found | Names right |
|---|---:|---:|---:|
| none | 2.3 | 63% | 92% |
| light | 1.6 | 57% | 81% |
| moderate | 1.1 | 58% | 50% |
| heavy | 0.5 | 68% | 29% |
| very heavy | 0.27 | 67% | 21% |
| near-opaque | 0.12 | 33% | 0% |

Counting holds up far longer than naming, which sets the two cutoffs: hazy below 1.4 (count only) and
too murky below 0.2 (nothing). Real clear frames today score 2.1–3.1. Once there's murky weather, the
score can be checked against the pier's turbidity sensor (a good SQL exercise).

**Three more ideas, tested the same way:**

| Idea | Result | Used? |
|---|---|---|
| Test-time augmentation (classify flipped/re-cropped copies and average) | No gain in accuracy (79% vs 80%), fewer fish named | No |
| Averaging several looks at the same fish (proxy for following it across frames) | **85% of names right vs 80%**, fewer fish named | **Yes**: burst frames + visits |
| Local species priors from iNaturalist records near the pier | Not used on its own: iNaturalist reflects what divers photograph (6 sardine records, but sardines school here constantly). The camera-trained classifier learns this camera's real frequencies from review answers instead. | Its species list, yes |

**The species list.** iNaturalist research-grade records within 1.5 km of the pier showed locally
common fish missing from the list. The model can only answer with names it's given, so a zebra-perch
swimming by was *forced* into a wrong name. 13 were added (zebra-perch sea chub, giant kelpfish, ocean
whitefish, rockfishes, croakers, sanddab, lizardfish, greenling, cabezon, grunion, diamond stingray,
banded guitarfish), for 57 animals (three later removed, see below). On known-species photos of all 38
species tested:

| Species list | Photos of the original 25 | Photos of the 13 added | All 38 |
|---|---:|---:|---:|
| 44 animals | 75% of names right | 11% (no right name available) | 56% |
| 57 animals | 70% | 82% | **74%** |

The cost is that new labels sometimes "steal" answers: ocean whitefish took 3 and zebra-perch 4 of 456.

**Then the deep-water species were taken out again.** On the live cam, the small, yellow-tailed fish
schooling near the pilings went from mostly "blacksmith" to mostly "ocean whitefish". The field guides
settle it:
- Juvenile blacksmith are blue-grey in front and bright yellow-orange behind until ~5 cm
  ([Aquarium of the Pacific](https://www.aquariumofpacific.org/onlinelearningcenter/species/blacksmith)),
  and school in midwater around structure.
- Ocean whitefish live 10–91 m down, mostly 24–55 m, near the bottom
  ([CDFW](https://marinespecies.wildlife.ca.gov/ocean-whitefish/the-species/)). They are unlikely at 4 m.

The iNaturalist records that suggested ocean whitefish and rockfish come from dives in the nearby La
Jolla Canyon. A direct test confirmed it: 10 photos of *juvenile* blacksmith, degraded to camera
quality, came out "ocean whitefish" 11 times out of 30 (confidently) and "blacksmith" only 6. Without
the three deep-water species (ocean whitefish, vermilion and brown rockfish), no confident wrong names
were left: 7 blacksmith, 6 leaning blacksmith but below the cutoff, the rest unsure. That left 54
animals, all plausible at ~4 m.

**Then what's actually been seen on this camera.** The cam's [highlight
clips](https://hdontap.com/stream/018408/scripps-pier-underwater-live-webcam/clips/highlight/), the
Scripps/CoOL pages, news stories and viewer forums list:
- a **sea turtle** (clip from 2026-09-22; La Jolla Shores has resident green turtles)
- octopus, seals, a stingray, leopard sharks, cormorants diving, lobsters, giant sea bass
- a baby garibaldi, mysid shrimp swarms, and swimmers

Fishing and diving reports add seasonal and El Niño visitors: mackerel, bonito, barracuda and yellowtail
(all reported at La Jolla in 2026), croakers, pelagic red crabs (they swarmed La Jolla Shores in the
2015 El Niño), pufferfish and triggerfish.

17 candidates were tested the same way, on photos of every current and candidate species:

| Species list | Photos of current species | Photos of candidates | Juvenile blacksmith | All |
|---|---:|---:|---:|---:|
| 54 animals | 77% of names right | 8% | 6/20 | 57% |
| + candidates | 78% | 72% | 6/20 | **76%** |

- **15 were added:** green sea turtle (right 10 of 12), market squid (9/12), queenfish (8/12),
  finescale triggerfish (7/12), chub and jack mackerel, bonito, barracuda, yellowtail, white seaperch,
  barred surfperch, white and yellowfin croaker, pelagic red crab and bullseye pufferfish. Most took 0–1
  answers from other species.
- **2 were left out:** thornback ray (never right, took 2 answers) and olive ridley turtle (always
  called green sea turtle, so it added nothing).
- **Also added:** "swimmer or snorkeler", next to scuba diver.

The list was then **70 animals**.

**Adding species from Scripps' dive counts (2026-09-26).** A Scripps survey of La Jolla's fishes
([Hastings et al. 2014](https://cmbc.ucsd.edu/wp-content/uploads/sites/399/2015/07/Hastings-et-al-2014-Fishes-of-La-Jolla-MPAs-.pdf))
counted over 90,000 fish in 500 dive transects at La Jolla Cove and Boomers. Blacksmith and señorita were
70% of them, and seven species 93%. Six of those seven were on the list; the seventh, **rock wrasse**
(seen in every survey period), wasn't. Neither were kelp perch, dwarf perch, rainbow surfperch, kelp
rockfish or tubesnout. (Zebraperch was: it's `zebra-perch sea chub`, *Kyphosus azureus*, its newer name.)

[`tracker/ml/species_eval.py`](../tracker/ml/species_eval.py) tests candidates from
[`species_candidates.json`](../tracker/ml/species_candidates.json) with the tracker's own naming rule, on
832 camera-degraded crops of underwater reference photos of 40 fish. Scores are **weighted by the dive
counts**, so a candidate that takes señorita or blacksmith names costs what it would on this camera,
where they're most of the fish. A candidate is added if the weighted score rises, the species already
listed don't lose more than 1 point, and the share of logged names that are right doesn't drop.

| Candidate | Its own photos named right | Verdict |
|---|---:|---|
| rock wrasse | 88% | At 0.85 it took 4 of 72 blacksmith: on this camera about half its names would be wrong. **Added with a 0.97 cutoff** (below it, the wrasse group) |
| rainbow surfperch | 29% | **Added**: helps a little, takes nothing |
| kelp perch | 67% | Left out: takes blacksmith, opaleye and giant kelpfish names at any cutoff |
| tubesnout | 44% | Left out: no gain |
| kelp rockfish, dwarf perch | 4 and 2 crops | Left out: too few underwater photos to judge |

With both added, on the same crops: named right, weighted **23.0% → 29.7%**; species already listed
23.9% → 28.1% (rock wrasse's label also pulls señorita and sheephead look-alikes into the wrasse group
instead of a wrong name); right, of the names logged, 63.8% → 66.8%; false alarms on the camera's
background 20.3% → 19.7%; young blacksmith unchanged. The 0.97 was chosen on these same crops, so expect
a smaller gain live; review answers are the real check.

The list is now **72 animals**.

**Also looked at:**
- NOAA's [AI for protected species](https://www.fisheries.noaa.gov/new-england-mid-atlantic/science-data/using-artificial-intelligence-study-protected-species)
  uses the same human-in-the-loop active learning as the review queue.
- [Li et al. 2025](https://doi.org/10.1109/JOE.2024.3455565) pretrain on unlabeled underwater footage
  (see Next steps; it's the reason for the frame bank).

These are synthetic tests, closer to the camera than clean photos but not the real thing. The review
answers are the real measure, and they're what the **Checked** column shows.

That last row is why every name in the results carries a **Checked** column, and why the review window
samples confident names too. Until people check them, the names are the model's guesses.

## 4. Things that sway: an audit of the first day's rare sightings

The rarer names from the first live day were checked by eye against their saved crops, review pictures
and frame-bank frames. Most were real fish (some with a doubtful species). But **every confident sighting
of a big or unusual animal came from two fixed things swaying in the surge**:
- growth hanging off the crossbeam, logged as giant sea bass, leopard shark, shovelnose guitarfish, rays
  and kelp bass;
- a round growth on the right-hand piling, unchanged from 7:00 to at least 11:40, logged as sheep crab
  and green sea turtle (up to 100%).

They moved enough to pass the motion check, and the model was sure of itself (90–100%). What was tried:

1. **"Same place, similar look, again later."** Not safe: different real fish passing the same spot
   looked as much alike (up to 0.80) as a fixture did to itself (0.76–0.91).
2. **The long-term background alone** (ignore a crop that matches it). On the first 68 review pictures:
   16 of 17 fixtures caught, no real fish lost. Live, it let about a third of the fixtures through
   (swaying and camera movement), and a lower cutoff would have hidden the resident kelp bass by the
   round growth (up to 0.835). Worse, it **hid a real spiny lobster**: sitting in its crevice at the top
   of the near piling, the lobster had become part of the background. A viewer saw its antennae at
   12:21; the tracker had ignored it as structure, while the model called those very crops "California
   spiny lobster" at 98–100%.
3. **A gallery of crops the background check ignored.** Caught the swaying growth well, but took the
   lobster in with it, for the same reason.
4. **Person-confirmed structure only** (what runs now). Checked on the day's labeled review pictures,
   each fixture compared only with the *other* confirmed ones:

| | Ignored as structure |
|---|---:|
| Confirmed fixtures (22) | 21 |
| Real fish (59), incl. the resident kelp bass | **0** |
| The spiny lobster in its crevice (12:35) | **0**, logged |

At places nobody has confirmed, anything that looks like the background goes to the review queue
instead of being logged, so it's never silently hidden and never counted by mistake. The first day's
fixture sightings (26 rows) were taken out of the database after checking each by eye; they're kept in
`data/archive/fixtures-2026-09-25/` with the reason for each.

## 5. Ongoing hand-checks on live footage

Clear reference photos flatter any model. The real test is the live cam, which is often green and murky.
So the tracker keeps **one sample crop per animal type every 10 minutes** in `data/crops/<date>/`, named
`<time>_<animal>_<confidence>.jpg`. To check a day:

1. Open `data/crops/<date>` in File Explorer with **Large icons** view.
2. Create a folder named `wrong` in it, even if everything turns out right. This marks the day as checked.
3. Move every crop whose name is wrong into `wrong`.

The next daily publish counts the checked crops and shows precision per animal in the **Validation** table
in the results above. Precision is the share of names that were right. The tallies are kept in
[`results/validation.csv`](../results/validation.csv) even after old crops are deleted (after 14 days).
Until some days are checked, the results section says the names are unverified.

## 6. Uncertain sightings, checked by a person

The review window is the main check. Answers to **"Is this right?"** pictures become the **Checked**
column and the **Validation** table. **"Not sure"** pictures that a person approves are listed in
**Confirmed by hand**, separately from the automatic counts. That's also how rarer animals get recorded
when the model hesitates.

**Improving it:** checked crops and approved review pictures are exactly the training data needed to go
past zero-shot. About 50 per species are enough to train a small classifier on BioCLIP's image embeddings,
which usually beats zero-shot by a wide margin on a specific camera.

## 7. A real octopus, and 18 false ones (2026-09-27 and 28)

On 2026-09-27 at about 17:52, a person watching the cam saw an octopus jet across the screen from the right,
in about 2 seconds. The tracker had frames at 17:52:45 and 17:52:57: open water in both, nothing moving.
With a snapshot every 10 s, a 2-second visitor is missed about 5 times in 6. The cam's own player keeps
only the last 30 s, so the moment couldn't be watched again either. That led to two changes: frames every
2 s, with the frames between snapshots checked for anything new and solid that moved
([How it works](HOW_IT_WORKS.md), step 9), and the video kept on the PC for rewinding.

Meanwhile, the tracker itself had logged **"octopus" 18 times** since 16:12 that day, at 90–99%
confidence. Every one was checked against its picture or the frames around it. None was an octopus:

| What it was | Sightings | How it's handled now |
|---|---:|---|
| Moving patches of empty open water (flickering light) | 5 | Skipped: no outline (detail 0.28–0.45; every kind of fish in the saved crops ≥ 0.64) |
| Grainy grey sensor noise at dusk and dawn | 4 | Twilight counts as night (colour saturation 70–110; daylight ≥ 175) |
| The resident lobster's antennae sweeping across the lens | 7 | Corrected to lobster; its look-alike rule catches most antenna pictures |
| The growth hanging off the crossbeam, curled up | 1 | A confirmed fixture: its place is in the gallery |
| A dark blur by the lens, nothing identifiable | 1 | Corrected to "not an animal" |

All 18 are corrected in `data/corrections.csv` (the logged name stays in the database, as always), and
the 9 "octopus" pictures in the review queue were the same kinds of thing. BioCLIP answers "octopus" for
featureless patches, probably because octopuses camouflage: many photos of them show texture and little
else. So an "octopus" from this tracker should be checked in its picture or the rewind before it's
believed.