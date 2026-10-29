"""
Tests for the optional timing and update-thread fields of a seed record
("minutes_ago" and "comments" - IncidentRepository._apply_seed_timeline in
app/repository.py).

Run from backend/:  python -m pytest tests/test_seed_comments.py -v
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models import Role, User
from app.models.comment import MAX_COMMENT_LENGTH
from app.repository import IncidentRepository, UserStore


@pytest.fixture
def users():
    store = UserStore()
    store.add(User("admin", Role.ADMIN, "Passw0rd!1"))
    store.add(User("tech1", Role.USER, "Passw0rd!2"))
    return store


def fault(**extra):
    record = {"id": "f-1", "kind": "fault", "title": "API down", "description": "503s", "created_by": "admin",
              "details": {"system_unavailable": True}}
    record.update(extra)
    return json.dumps(record)


def load(users, line):
    repo = IncidentRepository()
    result = repo.load_from_jsonl_lines([line], users)
    return repo, result


def close_to(moment, minutes_ago):
    expected = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return abs((moment - expected).total_seconds()) < 5


def test_comments_are_added_in_order_with_their_times(users):
    repo, result = load(users, fault(minutes_ago=120, comments=[
        {"author": "tech1", "text": "Looking.", "minutes_ago": 90},
        {"author": "admin", "text": "Fixed the config."},
    ]))
    incident = repo.get("f-1")
    assert [(c.author.username, c.text) for c in incident.comments] == [("tech1", "Looking."), ("admin", "Fixed the config.")]
    assert close_to(incident.created_at, 120)
    assert close_to(incident.comments[0].created_at, 90)
    assert close_to(incident.comments[1].created_at, 0)
    assert incident.updated_at == incident.comments[-1].created_at


def test_without_minutes_ago_the_report_time_follows_the_oldest_comment(users):
    repo, _ = load(users, fault(comments=[{"author": "tech1", "text": "Old note.", "minutes_ago": 300}]))
    incident = repo.get("f-1")
    assert incident.created_at == incident.comments[0].created_at


def test_records_without_the_new_fields_load_as_before(users):
    repo, result = load(users, fault())
    assert result.created_ids == ["f-1"] and repo.get("f-1").comments == []
    assert close_to(repo.get("f-1").created_at, 0)


@pytest.mark.parametrize("extra, reason", [
    ({"comments": "not a list"}, "must be a list"),
    ({"comments": ["text"]}, "comment 1 must be a JSON object"),
    ({"comments": [{"author": "ghost", "text": "x"}]}, "comment 1 'author' 'ghost' is not an account here"),
    ({"comments": [{"text": "x"}]}, "comment 1 'author'"),
    ({"comments": [{"author": "tech1", "text": "  "}]}, "comment 1: Comment text cannot be empty"),
    ({"comments": [{"author": "tech1", "text": 5}]}, "needs a 'text' string"),
    ({"comments": [{"author": "tech1", "text": "x" * (MAX_COMMENT_LENGTH + 1)}]}, "at most"),
    ({"comments": [{"author": "tech1", "text": "x", "minutes_ago": -1}]}, "0 or more"),
    ({"comments": [{"author": "tech1", "text": "x", "minutes_ago": True}]}, "0 or more"),
    ({"minutes_ago": "yesterday"}, "the record's 'minutes_ago'"),
    ({"comments": [{"author": "tech1", "text": "a", "minutes_ago": 5},
                   {"author": "tech1", "text": "b", "minutes_ago": 10}]}, "list comments oldest first"),
    ({"minutes_ago": 10, "comments": [{"author": "tech1", "text": "a", "minutes_ago": 20}]}, "older than the incident"),
])
def test_bad_timeline_fields_skip_the_record_with_a_reason(users, extra, reason):
    _, result = load(users, fault(**extra))
    assert result.created == 0
    assert reason in result.skipped_invalid[0]


def test_the_bundled_complex_sample_faults_have_comment_threads():
    path = Path(__file__).resolve().parents[2] / "data" / "sample_data.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    with_threads = [r for r in records if r.get("comments")]
    assert len(with_threads) >= 8
    assert all(r["kind"] == "fault" and len(r["comments"]) >= 3 for r in with_threads)


# --- absolute times: "created_at" (ISO 8601 with a time zone) ------------------

def iso_minutes_ago(minutes, suffix="Z"):
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + suffix


@pytest.mark.parametrize("suffix", ["Z", "+00:00"])
def test_absolute_times_set_the_report_and_comment_times(users, suffix):
    repo, result = load(users, fault(created_at=iso_minutes_ago(1440, suffix), comments=[
        {"author": "tech1", "text": "Looking.", "created_at": iso_minutes_ago(600, suffix)}]))
    incident = repo.get("f-1")
    assert result.created == 1
    assert close_to(incident.created_at, 1440) and close_to(incident.comments[0].created_at, 600)


def test_an_absolute_time_in_another_zone_is_the_same_moment(users):
    moment = (datetime.now(timezone(timedelta(hours=3))) - timedelta(hours=5)).replace(microsecond=0)
    repo, _ = load(users, fault(created_at=moment.isoformat()))
    assert repo.get("f-1").created_at == moment.astimezone(timezone.utc)


@pytest.mark.parametrize("extra, reason", [
    ({"created_at": "2026-10-16 14:30"}, "needs a time zone"),
    ({"created_at": "yesterday"}, "must be an ISO 8601 date and time"),
    ({"created_at": 12}, "must be an ISO 8601 date and time"),
    ({"created_at": "2999-01-01T00:00:00Z"}, "is in the future"),
    ({"created_at": "2026-01-01T00:00:00Z", "minutes_ago": 5}, "not both"),
])
def test_bad_absolute_times_skip_the_record(users, extra, reason):
    _, result = load(users, fault(**extra))
    assert result.created == 0 and reason in result.skipped_invalid[0]


def test_the_bundled_sample_data_uses_absolute_times():
    path = Path(__file__).resolve().parents[2] / "data" / "sample_data.jsonl"
    text = path.read_text(encoding="utf-8")
    assert "minutes_ago" not in text
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert all("created_at" in r for r in records)
