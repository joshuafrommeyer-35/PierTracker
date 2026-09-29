"""The published numbers: encounters (30-minute rule), look-alike group types, and whose answers count."""
from contextlib import closing
from dataclasses import dataclass

import db
import publish_results as pr


@dataclass
class Sighting:
    common: str
    count: int = 1
    confidence: float = 0.9
    method: str = "zero-shot"


def test_encounters_merge_sightings_less_than_30_minutes_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "STATS_FROM", "2026-01-01T00:00:00")
    with closing(db.connect()) as con:
        for t in ("10:00:00", "10:10:00", "10:29:00", "11:05:00", "11:06:00"):  # one fish lingering, then back later
            db.record_snapshot(con, f"2026-09-25T{t}", False, [Sighting("kelp bass")])
    effort, species, _ = pr.load_hourly()
    assert species[("2026-09-25", 10, "kelp bass")][0] == 3  # snapshots: every one counts
    assert pr.load_encounters(effort)[("2026-09-25", "kelp bass")] == 2  # encounters: 10:00 and 11:05


def test_an_animal_seen_only_between_snapshots_is_listed(tmp_path, monkeypatch):
    """An octopus jetting across in 2 s, caught in a frame between two snapshots: an encounter, 0 snapshots."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "STATS_FROM", "2026-01-01T00:00:00")
    with closing(db.connect()) as con:
        db.record_snapshot(con, "2026-09-27T17:52:40", False, [])
        db.record_snapshot(con, "2026-09-27T17:52:44", False, [Sighting("octopus")], regular=False)
        db.record_snapshot(con, "2026-09-27T17:52:50", False, [])
    effort, species, _ = pr.load_hourly()
    days, by_hour = pr.build(effort, species)
    encounters = pr.load_encounters(effort)
    assert days["2026-09-27"]["analyzed"] == 2
    text = pr.render(days, by_hour, {}, {}, 0, [], encounters)
    assert "| octopus | 1 | 0 | 2026-09-27 | 2026-09-27 |" in text  # seen briefly: 1 encounter, 0 snapshots


def test_the_wording_explains_head_counts_and_the_camera_classifier():
    text = pr.render({"2026-09-25": {"analyzed": 10, "dark": 0, "murky": 0, "species": {"kelp bass": [10, 2]}}},
                     [0] * 24, {}, {}, 0, [], {("2026-09-25", "kelp bass"): 1})
    assert "individual" not in text.split("### Animals seen")[1].split("|")[0].replace("recognize a particular", "")
    assert "wasn't trained on this camera" not in text
    assert "blacksmith is closest with 10" in pr.training_line({"not an animal": 43, "blacksmith": 10, "salema": 2})
    assert "So far 1 has enough (blacksmith)" in pr.training_line({"blacksmith": 12, "salema": 3})


def test_look_alike_groups_have_a_type():
    cats = pr.categories()
    assert cats["silversides & sardines"] == "fish"
    assert cats["fish (unidentified)"] == "fish"


def test_only_a_persons_answers_count_as_checks(tmp_path, monkeypatch):
    decisions = tmp_path / "decisions.csv"
    decisions.write_text(
        "reviewed_at,taken_at,image,kind,logged_as,decision,common_name,scientific_name,category,best_guess,best_guess_prob,reviewer\n"
        '"x","2026-09-25T10:00:00","a.jpg","check","kelp bass","approved","kelp bass","","","kelp bass","0.9","person"\n'
        '"x","2026-09-25T10:05:00","b.jpg","check","sheep crab","rejected","","","","sheep crab","0.9","claude"\n'
        '"x","2026-09-25T10:06:00","c.jpg","uncertain","","approved","California spiny lobster","","","octopus","0.5","claude"\n',
        encoding="utf-8")
    monkeypatch.setattr(pr, "DECISIONS_CSV", decisions)
    monkeypatch.setattr(pr, "RESULTS", tmp_path)
    monkeypatch.setattr(pr, "CONFIRMED_CSV", tmp_path / "confirmed.csv")
    confirmed, rejected, checks = pr.read_reviews()
    assert dict(checks) == {"kelp bass": [1, 0]}  # Claude's answers teach the tracker but aren't published as checks
    assert not confirmed and rejected == 0
    assert (tmp_path / "confirmed.csv").read_text(encoding="utf-8").count("claude") == 2  # but they're listed, marked


def test_one_off_guesses_are_listed_separately():
    days = {"2026-09-25": {"analyzed": 1000, "dark": 0, "murky": 0,
                           "species": {"kelp bass": [30, 1], "Pacific barracuda": [1, 1], "garibaldi": [2, 1]}}}
    encounters = {("2026-09-25", "kelp bass"): 1, ("2026-09-25", "Pacific barracuda"): 1, ("2026-09-25", "garibaldi"): 1}
    confirmed = {"garibaldi": [1, "2026-09-25"]}  # a person approved one
    text = pr.render(days, [0] * 24, {}, confirmed, 0, [], encounters)
    main, _, rest = text.partition("<details><summary>Seen briefly")
    assert "| kelp bass |" in main and "| garibaldi |" in main   # seen repeatedly / confirmed by a person
    assert "Pacific barracuda" not in main and "| Pacific barracuda |" in rest  # a one-off guess
