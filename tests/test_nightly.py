"""The nightly job runs once an evening: LiveCams starting it again (a restart, waking up) does nothing."""
import nightly


def test_nightly_runs_once_an_evening(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LAST_RUN", tmp_path / ".nightly_last_run")
    assert nightly.claim("2026-09-25")
    assert not nightly.claim("2026-09-25")  # started again after a restart
    assert nightly.claim("2026-09-26")      # the next evening
