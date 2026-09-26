"""The nightly job runs once a day: LiveCams starting it again (a restart, waking up) does nothing."""
from datetime import date

import nightly


def test_nightly_runs_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LAST_RUN", tmp_path / ".nightly_last_run")
    assert nightly.first_run_today()
    assert not nightly.first_run_today()  # started again the same day


def test_nightly_runs_again_the_next_day(tmp_path, monkeypatch):
    marker = tmp_path / ".nightly_last_run"
    marker.write_text("2026-09-24", encoding="utf-8")
    monkeypatch.setattr(nightly, "LAST_RUN", marker)
    assert nightly.first_run_today()
    assert marker.read_text(encoding="utf-8") == date.today().isoformat()
