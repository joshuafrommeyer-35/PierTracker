"""Tests species before they join species.json: would naming them help the tracker or hurt it?

For each candidate in species_candidates.json it gets reference photos from iNaturalist and turns the
held-out ones into test crops that look like this camera (reference_photos.py fetch/embed), makes its
label the way setup_models.py does, and runs the tracker's own rule (a species at 0.85, a look-alike
group at 0.90) on the test crops of every fish, with and without the candidate.

Scores are weighted by how common each fish is on La Jolla's shallow reefs: divers' counts per 100 m²
at La Jolla Cove, 2002-2005 (Hastings et al. 2014, Bull. South. Calif. Acad. Sci. 113(3), Table 3).
So a candidate that steals señoritas costs what it would on this camera, where they're a quarter of
all fish. A candidate is added if the weighted score rises, the species already on the list don't
lose more than MAX_LOSS, and names don't get less trustworthy (right of those named) by more than
MAX_LOSS either.

    tracker\\.venv\\Scripts\\python tracker\\ml\\species_eval.py    # fetch + embed (~15 min) + test

Writes data/ml/species_eval_report.md.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference_photos as R  # noqa: E402

CANDIDATES = Path(__file__).resolve().parent / "species_candidates.json"
LABELS = R.LIVECAMS / "data" / "ml" / "candidate_labels.npz"
REPORT = R.LIVECAMS / "data" / "ml" / "species_eval_report.md"
SPECIES_CUT, GROUP_CUT = 0.85, 0.90   # the tracker's SPECIES_MIN_PROB / GROUP_MIN_PROB
MAX_LOSS = 0.01
RARE = 0.05                           # weight of a species the divers didn't count (or counted 0)
DENSITY = {  # fish per 100 m², La Jolla Cove 2002-2005 means (Hastings et al. 2014, Table 3)
    "blacksmith": 82.36, "senorita": 55.33, "garibaldi": 13.59, "kelp bass": 11.82, "opaleye": 8.47,
    "California sheephead": 5.99, "rock wrasse": 5.90, "black perch": 1.79, "barred sand bass": 1.66,
    "halfmoon": 1.61, "sargo": 1.43, "jacksmelt": 1.15, "salema": 0.83, "Pacific barracuda": 0.69,
    "kelp perch": 0.68, "walleye surfperch": 0.58, "dwarf perch": 0.36, "zebra-perch sea chub": 0.30,
    "giant kelpfish": 0.28, "tubesnout": 0.28, "bat ray": 0.27, "kelp rockfish": 0.26,
    "rainbow surfperch": 0.22, "topsmelt": 0.17, "white seaperch": 0.15, "pile perch": 0.14,
    "shiner perch": 0.13, "rubberlip seaperch": 0.12, "barred surfperch": 0.09, "painted greenling": 0.06,
    "giant sea bass": 0.06,
}


def is_fish(sp):
    return sp["category"] in ("fish", "shark/ray")


def candidate_labels(candidates):
    """Label embeddings for the candidates, made like setup_models.py makes them (kept for re-runs)."""
    names = [c["common"] for c in candidates]
    if LABELS.exists():
        d = np.load(LABELS)
        if list(d["names"]) == names:
            return d["emb"]
    import open_clip
    import torch
    import torch.nn.functional as F
    import setup_models as S
    clip, _, _ = open_clip.create_model_and_transforms(S.BIOCLIP)
    tok = open_clip.get_tokenizer(S.BIOCLIP)
    out = []
    with torch.no_grad():
        for c in candidates:
            e = F.normalize(clip.eval().encode_text(tok([t.format(**c) for t in S.SPECIES_TEMPLATES])), dim=-1).mean(0)
            out.append(F.normalize(e, dim=0).numpy())
    emb = np.array(out, np.float32)
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    np.savez(LABELS, names=np.array(names), emb=emb)
    return emb


def test_crops(species):
    """(embedding, species, juvenile?) for every crop of an underwater photo. All of them, not only the
    held-out fifth: naming by names alone learns nothing from these photos, so all are fair tests, and
    the common species need many crops (a few señoritas would swing the weighted score)."""
    ft, uw_mask = R.filter_text()
    scale = json.loads((R.TRACKER / "models" / "classifier.json").read_text(encoding="utf-8"))["logit_scale"]
    crops = []
    for sp in species:
        f = R.EMB / f"{R.folder_name(sp['common'])}.npz"
        if not f.exists():
            continue
        d = np.load(f)
        z = scale * d["full"] @ ft.T
        p = np.exp(z - z.max(1, keepdims=True))
        underwater = (p[:, uw_mask].sum(1) / p.sum(1)) >= R.UNDERWATER_MIN
        for e, k in zip(d["deg"], d["deg_photo"]):
            if underwater[k] or d["split"][k] == "test_juvenile":
                crops.append((e, sp["common"], d["split"][k] == "test_juvenile"))
    return crops


class Rule:
    """The tracker's naming rule over one set of labels (fish + "not an animal" labels), including
    species.json's stricter per-species cutoffs (min_prob)."""

    def __init__(self, names, emb, negative, group_of, scale, min_prob=None):
        self.names, self.E, self.negative, self.group_of, self.scale = names, emb, negative, group_of, scale
        self.min_prob = min_prob or {}
        self.animal = np.where(~negative)[0]

    def call(self, e):
        z = self.scale * self.E @ e
        p = np.exp(z - z.max())
        p /= p.sum()
        best = self.animal[int(p[self.animal].argmax())]
        if p[best] >= max(SPECIES_CUT, self.min_prob.get(self.names[best], 0)):
            return self.names[best]
        totals = defaultdict(float)
        for i in self.animal:
            if self.group_of.get(self.names[i]):
                totals[self.group_of[self.names[i]]] += p[i]
        if totals:
            g, v = max(totals.items(), key=lambda kv: kv[1])
            if v >= GROUP_CUT:
                return g
        return None


def score(rule, crops, group_of, bg):
    """Per species: share of crops named right (species or its group) and named at all; overall numbers."""
    per = defaultdict(lambda: [0, 0, 0])  # right, named, crops
    juvenile = [0, 0, 0]  # right, wrong, crops
    stolen = defaultdict(int)  # (true species, wrong name) -> crops
    for e, true, juv in crops:
        name = rule.call(e)
        right = name is not None and (name == true or name == group_of.get(true))
        if juv:
            juvenile[0] += right
            juvenile[1] += name is not None and not right
            juvenile[2] += 1
            continue
        per[true][0] += right
        per[true][1] += name is not None
        per[true][2] += 1
        if name is not None and not right:
            stolen[(true, name)] += 1
    alarms = float(np.mean([rule.call(e) is not None for e in bg]))
    return per, juvenile, stolen, alarms


def summary(per, species):
    """Weighted and plain: right of all, right of those named."""
    w = {s: DENSITY.get(s, 0) or RARE for s in per}
    sel = [s for s in per if s in species]
    if not sel:
        return {}
    acc = {s: per[s][0] / per[s][2] for s in sel}
    named_w = sum(w[s] * per[s][1] / per[s][2] for s in sel)
    return {
        "weighted": sum(w[s] * acc[s] for s in sel) / sum(w[s] for s in sel),
        "weighted_trust": sum(w[s] * per[s][0] / per[s][2] for s in sel) / max(named_w, 1e-9),
        "plain": float(np.mean([acc[s] for s in sel])),
    }


def main():
    from tracker import enter_efficiency_mode
    enter_efficiency_mode()
    current = R.species_list()
    candidates = json.loads(CANDIDATES.read_text(encoding="utf-8"))["candidates"]
    have = {s["common"] for s in current}
    candidates = [c for c in candidates if c["common"] not in have]
    missing = [c for c in candidates if not (R.EMB / f"{R.folder_name(c['common'])}.npz").exists()]
    if missing:
        R.species_list = lambda: current + missing  # fetch/embed then only do what's missing
        print("fetching reference photos ...", flush=True)
        R.fetch()
        print("embedding ...", flush=True)
        R.embed()

    meta = json.loads((R.TRACKER / "models" / "classifier.json").read_text(encoding="utf-8"))
    labels, scale = meta["labels"], meta["logit_scale"]
    E = np.load(R.TRACKER / "models" / "label_embeddings.npy")
    idx = [i for i, l in enumerate(labels) if l["negative"] or l["category"] in ("fish", "shark/ray")]
    base_names = [labels[i]["common"] for i in idx]
    base_E = E[idx] / np.linalg.norm(E[idx], axis=1, keepdims=True)
    base_neg = np.array([labels[i]["negative"] for i in idx])
    cand_E = candidate_labels(candidates)
    group_of = {s["common"]: s.get("group") for s in current + candidates if s.get("group")}
    min_prob = {s["common"]: s["min_prob"] for s in current + candidates if s.get("min_prob")}
    fish_now = {s["common"] for s in current if is_fish(s)}

    def rule(chosen):
        names = base_names + [c["common"] for c in chosen]
        rows = [cand_E[candidates.index(c)] for c in chosen]
        emb = np.vstack([base_E] + rows) if rows else base_E
        neg = np.concatenate([base_neg, np.zeros(len(chosen), bool)])
        return Rule(names, emb, neg, {n: g for n, g in group_of.items() if n in names}, scale, min_prob)

    crops = test_crops([s for s in current + candidates if is_fish(s)])
    bg = np.load(R.EMB / "_background.npz")["emb"]
    everyone = fish_now | {c["common"] for c in candidates}

    base = score(rule([]), crops, group_of, bg)
    b_all, b_now = summary(base[0], everyone), summary(base[0], fish_now)
    lines = [f"# Species candidates ({datetime.now():%Y-%m-%d})", "",
             f"Test crops: {sum(v[2] for v in base[0].values())} held-out underwater photos of "
             f"{len(base[0])} fish, degraded to look like this camera. Weighted by La Jolla Cove dive counts "
             "(Hastings et al. 2014); uncounted species weigh " + f"{RARE}.", "",
             "| Candidate | Its own photos named right | Weighted, all fish | Species already listed | "
             "Right of those named (weighted) | Verdict |",
             "|---|---:|---:|---:|---:|---|",
             f"| (list as it is) | | {b_all['weighted']:.1%} | {b_now['weighted']:.1%} | {b_all['weighted_trust']:.1%} | |"]
    accepted = []
    for c in candidates:
        per, _, stolen, _ = score(rule([c]), crops, group_of, bg)
        s_all, s_now = summary(per, everyone), summary(per, fish_now)
        own = per[c["common"]][0] / max(per[c["common"]][2], 1)
        ok = (s_all["weighted"] > b_all["weighted"] and s_now["weighted"] >= b_now["weighted"] - MAX_LOSS
              and s_all["weighted_trust"] >= b_all["weighted_trust"] - MAX_LOSS)
        if ok:
            accepted.append(c)
        takes = [f"{t} {k} of {per[t][2]}" for (t, n), k in sorted(stolen.items(), key=lambda kv: -kv[1])
                 if n == c["common"]][:3]
        lines.append(f"| {c['common']} ({per[c['common']][2]} crops) | {own:.0%} | {s_all['weighted']:.1%} | "
                     f"{s_now['weighted']:.1%} | {s_all['weighted_trust']:.1%} | "
                     f"{'add' if ok else 'leave out'}{' (takes: ' + ', '.join(takes) + ')' if takes else ''} |")
    if accepted:
        per, juv, _, alarms = score(rule(accepted), crops, group_of, bg)
        s_all, s_now = summary(per, everyone), summary(per, fish_now)
        lines += ["", f"**Together** ({', '.join(c['common'] for c in accepted)}): weighted {b_all['weighted']:.1%} -> "
                  f"{s_all['weighted']:.1%}; species already listed {b_now['weighted']:.1%} -> {s_now['weighted']:.1%}; "
                  f"right of those named {b_all['weighted_trust']:.1%} -> {s_all['weighted_trust']:.1%}; unweighted "
                  f"{b_all['plain']:.1%} -> {s_all['plain']:.1%}. False alarms on this camera's background "
                  f"{base[3]:.1%} -> {alarms:.1%}. Young blacksmith right / wrong: {base[1][0]}/{base[1][1]} -> "
                  f"{juv[0]}/{juv[1]} (of {juv[2]})."]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("ACCEPTED", json.dumps([c["common"] for c in accepted]))


if __name__ == "__main__":
    main()
