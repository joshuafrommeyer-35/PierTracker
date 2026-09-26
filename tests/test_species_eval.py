"""The species test uses the tracker's naming rule, and weights species by how common they are here."""
import numpy as np

from ml import species_eval as se


def rule():
    """Three labels on orthogonal axes: two surfperches (a look-alike group) and "not an animal"."""
    names = ["black perch", "pile perch", "sandy sea floor"]
    group_of = {"black perch": "surfperches", "pile perch": "surfperches"}
    return se.Rule(names, np.eye(3), np.array([False, False, True]), group_of, scale=10.0)


def test_rule_names_a_species_only_when_sure():
    r = rule()
    assert r.call(np.array([1.0, 0.0, 0.0])) == "black perch"      # p ~ 1.0
    # Split between the two perches: neither reaches 0.85, but together they do: the group
    assert r.call(np.array([0.5, 0.5, -0.5])) == "surfperches"
    # Mostly "not an animal": nothing
    assert r.call(np.array([0.0, 0.0, 1.0])) is None


def test_a_stricter_cutoff_falls_back_to_the_group():
    r = rule()
    r.min_prob = {"black perch": 1.0}  # never sure enough on its own (rock wrasse has 0.97)
    # p(black perch) ~ 0.9999 isn't enough any more; the surfperch group still is
    assert r.call(np.array([1.0, 0.0, 0.0])) == "surfperches"


def test_the_tracker_honors_species_cutoffs():
    import tracker
    m = tracker.Models.__new__(tracker.Models)  # just the naming rule, without loading the models
    m.labels = [{"common": "rock wrasse", "negative": False}, {"common": "senorita", "negative": False},
                {"common": "kelp", "negative": True}]
    m.group_of = {"rock wrasse": "wrasses", "senorita": "wrasses"}
    m.groups = {"wrasses": {"common": "wrasses"}}
    m.min_prob = {"rock wrasse": 0.97}
    label, _ = m.name(np.array([0.95, 0.04, 0.01]), None, 0.85, 0.90)
    assert label["common"] == "wrasses"
    label, _ = m.name(np.array([0.04, 0.95, 0.01]), None, 0.85, 0.90)
    assert label["common"] == "senorita"


def test_common_fish_count_more():
    # A candidate that costs señoritas half their right answers but gets tubesnout right: señoritas are
    # ~200x as common on La Jolla's reefs, so the weighted score must drop even though the plain one rises.
    before = {"senorita": [10, 10, 10], "tubesnout": [0, 0, 10]}
    after = {"senorita": [5, 10, 10], "tubesnout": [10, 10, 10]}
    species = {"senorita", "tubesnout"}
    b, a = se.summary(before, species), se.summary(after, species)
    assert a["plain"] > b["plain"]
    assert a["weighted"] < b["weighted"]
