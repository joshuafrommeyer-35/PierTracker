"""The community viewer's server: config reading and what visitors can and can't submit."""
import json

import pytest
from PIL import Image

import server


def test_config_reader_strips_comments_but_not_urls(tmp_path, monkeypatch):
    (tmp_path / "livecams.json").write_text('''{
      // a comment
      "cams": [{"page": "https://coollab.ucsd.edu/pierviz/"}],  // trailing comment
      "community": {"port": 9000, "host": "127.0.0.1",},
    }''', encoding="utf-8")
    monkeypatch.setattr(server, "LIVECAMS", tmp_path)
    monkeypatch.setattr(server, "HERE", tmp_path)  # no secrets.json here
    config = server.read_config()
    assert config["port"] == 9000 and config["show_frames"] and not config["embed_allowed"]


def test_text_from_visitors_is_plain_and_formula_safe():
    assert server.clean("=HYPERLINK(\"x\")", 80).startswith("'")
    assert server.clean("kelp\nbass\x00", 80) == "kelp bass"
    assert len(server.clean("x" * 500, 80)) == 80


@pytest.fixture
def served(tmp_path, monkeypatch):
    recent = tmp_path / "recent"
    recent.mkdir()
    Image.new("RGB", (1920, 1080), (20, 120, 140)).save(recent / "130430.jpg")
    monkeypatch.setattr(server, "RECENT", recent)
    monkeypatch.setattr(server, "SUBMISSIONS", tmp_path / "submissions")
    monkeypatch.setitem(server.CONFIG, "drive_folder", "")
    monkeypatch.setitem(server.CONFIG, "apps_script_url", "")
    monkeypatch.setattr(server, "served", {"2026-09-25T13:04:30": [
        {"box": [656, 0, 1336, 752], "name": "diamond stingray", "prob": 0.966, "status": "logged"}]})
    monkeypatch.setattr(server, "recent_posts", server.defaultdict(server.deque))
    return tmp_path


def test_answers_only_about_boxes_the_tracker_showed(served):
    body = {"taken_at": "2026-09-25T13:04:30", "box": [0, 0, 100, 100], "community_id": "lobster"}
    assert server.save_submission(body, "1.2.3.4")[0] == 400
    body["box"] = [656, 0, 1336, 752]
    body["tracker_said"] = "something a visitor made up"
    assert server.save_submission(body, "1.2.3.4")[0] == 200
    row = (served / "submissions" / "submissions.csv").read_text(encoding="utf-8").splitlines()[1]
    assert "diamond stingray" in row and "made up" not in row  # the tracker's name comes from its own record
    assert len(list((served / "submissions").glob("*.jpg"))) == 1


def test_bad_times_and_rate_limit(served, monkeypatch):
    assert server.save_submission({"taken_at": "../../etc/passwd", "box": [1, 2, 3, 4], "community_id": "x"}, "a")[0] == 400
    monkeypatch.setitem(server.CONFIG, "per_hour", 2)
    body = {"taken_at": "2026-09-25T13:04:30", "box": [656, 0, 1336, 752], "community_id": "lobster"}
    codes = [server.save_submission(dict(body), "5.6.7.8")[0] for _ in range(3)]
    assert codes == [200, 200, 429]
