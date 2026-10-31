"""
Tests for the Server settings page's site and mailing-list imports
(POST /sites/import-jsonl, /sites/mailing-lists/import-jsonl in
app/api/sites.py): admin only, the report, ids already stored skipped.

Run from backend/:  python -m pytest tests/test_api/test_site_imports.py -v
"""

import json

from app.sites import DEFAULT_MAILING_LISTS_SEED, DEFAULT_SITES_SEED

SITES = "\n".join([json.dumps({"site_id": 1001, "site_name": "Portal", "site_url": "https://example.com"}),
                   json.dumps({"site_id": 1001, "site_name": "Again", "site_url": "https://example.net"}),
                   "{bad"])
LISTS = json.dumps({"list_id": "ops", "name": "Ops", "members": ["a@example.com"], "site_ids": [1001, 4242]})


def test_only_admins(user_client):
    assert user_client.post("/sites/import-jsonl", json={"content": SITES}).status_code == 403
    assert user_client.post("/sites/mailing-lists/import-jsonl", json={"content": LISTS}).status_code == 403


def test_the_report_and_a_second_import(admin_client):
    first = admin_client.post("/sites/import-jsonl", json={"content": SITES}).json()
    assert first["created_ids"] == [1001] and first["skipped_duplicate_ids"] == [1001]
    assert len(first["skipped_invalid"]) == 1 and first["skipped_invalid"][0].startswith("line 3")
    lists = admin_client.post("/sites/mailing-lists/import-jsonl", json={"content": LISTS}).json()
    assert lists["created_ids"] == ["ops"] and "4242" in lists["warnings"][0]
    again = admin_client.post("/sites/import-jsonl", json={"content": SITES}).json()
    assert again["created"] == 0 and again["already_stored_ids"] == [1001]
    assert [s["site_id"] for s in admin_client.get("/sites").json()] == [1001]


def test_the_sample_files(admin_client):
    sites = admin_client.post("/sites/import-jsonl", json={"content": DEFAULT_SITES_SEED.read_text("utf-8")}).json()
    lists = admin_client.post("/sites/mailing-lists/import-jsonl",
                              json={"content": DEFAULT_MAILING_LISTS_SEED.read_text("utf-8")}).json()
    assert sites["created"] >= 10 and lists["created"] > 0
    assert not (sites["skipped_invalid"] or lists["skipped_invalid"] or lists["warnings"])
