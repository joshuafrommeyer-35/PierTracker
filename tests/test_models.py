"""The conditions model on synthetic data where the truth is known."""
import numpy as np
import pandas as pd

from ml import conditions_model as cm


def synthetic(seed=1, days=40):
    """"leopard shark" shows up twice as often (odds) per SD of warm water; "kelp bass" shows up equally
    often but lingers ~4x longer when it's warm (many more snapshots, not more visits)."""
    rng = np.random.default_rng(seed)
    rows, env = [], []
    for day in range(days):
        d = f"2026-10-{day + 1:02d}" if day < 31 else f"2026-11-{day - 30:02d}"
        day_anom = rng.normal(2.5, 0.8)
        for hour in range(7, 19):
            anom = day_anom + rng.normal(0, 0.2)
            env.append({"date": d, "hour": hour, "pier_temp_anomaly_c": anom, "turbidity_ntu": rng.gamma(2, 1.5),
                        "chlorophyll_ug_l": rng.gamma(2, 1), "tide_predicted_m": np.sin(hour / 2 + day),
                        "tide_trend": int(np.cos(hour / 2 + day) > 0), "oni": 1.8})
            z = (anom - 2.5) / 0.8
            murky = int(rng.integers(0, 60))
            seen = {}
            if rng.random() < 1 / (1 + np.exp(-(-1.0 + np.log(2.0) * z))):
                seen["leopard shark"] = int(rng.integers(1, 20))
            if rng.random() < 0.35:
                seen["kelp bass"] = int(min(360 - murky, rng.geometric(1 / (30 * (4 if z > 0 else 1)))))
            base = {"date": d, "hour": hour, "snapshots_analyzed": 360, "snapshots_dark": 0, "snapshots_murky": murky}
            rows += [dict(base, common_name=n, snapshots_seen=k, max_count=1) for n, k in seen.items()] or \
                [dict(base, common_name="", snapshots_seen=0, max_count=0)]
    return rows, pd.DataFrame(env)


def test_presence_model_finds_real_effects_and_ignores_lingering(tmp_path, monkeypatch):
    """Over 5 synthetic data sets: the real warm-water effect is found (interval above 1) in at least 4,
    and the fish that only lingers longer is called an effect in at most 1 (the 5% chance rate)."""
    warm = "water warmer than normal"
    found = mistaken = 0
    for seed in range(1, 6):
        rows, env = synthetic(seed)
        env.to_csv(tmp_path / "env.csv", index=False)
        monkeypatch.setattr(cm, "CONDITIONS", tmp_path / "env.csv")
        table, _ = cm.load(rows)
        _, shark = cm.fit(table, "leopard shark")
        found += next(r for r in shark if r[0] == warm)[2] > 1.0
        _, bass = cm.fit(table, "kelp bass")
        _, _, low, high, _ = next(r for r in bass if r[0] == warm)
        mistaken += not (low <= 1.0 <= high)
    assert found >= 4 and mistaken <= 1
