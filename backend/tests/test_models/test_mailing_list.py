"""
Unit tests for app.models.mailing_list.MailingList (Sites & Mailing Lists
subsystem). Plain domain objects only - no HTTP, no database.

Run from backend/:  python -m pytest tests/test_models/test_mailing_list.py -v
Uses the shared admin_user / regular_user fixtures from tests/conftest.py.
"""

import pytest

from app.models import MailingList


@pytest.fixture
def ops() -> MailingList:
    return MailingList("ops-team", "Operations team",
                       members=["ops1@example.com", "Ops2@Example.com"], site_ids=[1043, 1042])


# --- construction and validation -------------------------------------

def test_new_list_normalizes_and_sorts(ops):
    assert ops.members == ("ops1@example.com", "ops2@example.com")
    assert ops.site_ids == (1042, 1043)
    assert len(ops) == 2
    assert not ops.is_archived


def test_duplicates_collapse():
    ml = MailingList("a", "A", members=["x@example.com", "X@example.com"], site_ids=[1, 1])
    assert ml.members == ("x@example.com",)
    assert ml.site_ids == (1,)


@pytest.mark.parametrize("bad_id", ["", "Ops-Team", "ops team", "-ops", "ops--team", "ops-", 5, None, "a" * 51])
def test_invalid_list_id_is_rejected(bad_id):
    with pytest.raises(ValueError, match="list_id"):
        MailingList(bad_id, "Name")


def test_list_id_is_read_only(ops):
    with pytest.raises(AttributeError):
        ops.list_id = "other"


@pytest.mark.parametrize("bad_name", ["", "  ", None, "n" * 101])
def test_invalid_name_is_rejected(ops, bad_name):
    with pytest.raises(ValueError, match="name"):
        ops.name = bad_name
    assert ops.name == "Operations team"


@pytest.mark.parametrize("bad_email", ["", "no-at-sign", "a@b", "two@@example.com", "sp ace@example.com", None, 7])
def test_invalid_email_is_rejected(ops, bad_email):
    with pytest.raises(ValueError, match="email"):
        ops.add_member(bad_email)


@pytest.mark.parametrize("bad_site", [0, -1, "1042", True, 1.5])
def test_invalid_site_id_is_rejected(ops, bad_site):
    with pytest.raises(ValueError, match="site ids"):
        ops.link_site(bad_site)


# --- collection operations -------------------------------------------

def test_add_member_reports_whether_it_was_new(ops):
    assert ops.add_member("new@example.com") is True
    assert ops.add_member("NEW@example.com") is False
    assert "new@example.com" in ops


def test_remove_member_raises_when_absent(ops):
    ops.remove_member("OPS1@example.com")
    assert "ops1@example.com" not in ops
    with pytest.raises(ValueError, match="not a member"):
        ops.remove_member("ops1@example.com")


def test_unlink_site_is_a_silent_no_op_when_absent(ops):
    ops.unlink_site(1042)
    ops.unlink_site(1042)  # discard semantics: no error the second time
    ops.unlink_site(9999)  # never linked: still no error
    assert ops.site_ids == (1043,)


def test_link_site_and_covers(ops):
    assert ops.covers(1042) and not ops.covers(2000)
    assert ops.link_site(2000) is True
    assert ops.link_site(2000) is False
    assert ops.covers(2000)


def test_contains_tolerates_invalid_input(ops):
    assert "not an email" not in ops
    assert None not in ops


def test_recipients_is_an_immutable_snapshot(ops):
    snapshot = ops.recipients
    ops.add_member("late@example.com")
    assert "late@example.com" not in snapshot
    assert isinstance(snapshot, frozenset)


# --- from_dict ---------------------------------------------------------

def test_from_dict_builds_a_list():
    ml = MailingList.from_dict({"list_id": "mgmt", "name": "Management",
                                "members": ["boss@example.com"], "site_ids": [1042]})
    assert (ml.list_id, ml.members, ml.site_ids) == ("mgmt", ("boss@example.com",), (1042,))


def test_from_dict_members_and_sites_are_optional():
    ml = MailingList.from_dict({"list_id": "empty", "name": "Empty"})
    assert ml.members == () and ml.site_ids == ()


@pytest.mark.parametrize("missing", ["list_id", "name"])
def test_from_dict_missing_required_field(missing):
    data = {"list_id": "mgmt", "name": "Management"}
    del data[missing]
    with pytest.raises(ValueError, match=f"missing required field '{missing}'"):
        MailingList.from_dict(data)


@pytest.mark.parametrize("field", ["members", "site_ids"])
def test_from_dict_requires_lists(field):
    with pytest.raises(ValueError, match=f"'{field}' must be a list"):
        MailingList.from_dict({"list_id": "mgmt", "name": "M", field: "not-a-list"})


def test_from_dict_error_names_the_record():
    with pytest.raises(ValueError, match="mailing list record 'mgmt'.*email"):
        MailingList.from_dict({"list_id": "mgmt", "name": "M", "members": ["broken"]})


def test_from_dict_rejects_non_objects():
    with pytest.raises(ValueError, match="JSON object"):
        MailingList.from_dict(["mgmt"])


# --- archive / restore -------------------------------------------------

def test_archive_and_restore_keep_members_and_sites(ops, admin_user):
    ops.archive(admin_user)
    assert ops.is_archived
    ops.restore(admin_user)
    assert not ops.is_archived
    assert ops.members == ("ops1@example.com", "ops2@example.com")
    assert ops.site_ids == (1042, 1043)


def test_archive_and_restore_state_errors(ops, admin_user):
    with pytest.raises(RuntimeError, match="not archived"):
        ops.restore(admin_user)
    ops.archive(admin_user)
    with pytest.raises(RuntimeError, match="already archived"):
        ops.archive(admin_user)


def test_only_admin_can_archive_and_restore(ops, admin_user, regular_user):
    with pytest.raises(PermissionError):
        ops.archive(regular_user)
    ops.archive(admin_user)
    with pytest.raises(PermissionError):
        ops.restore(regular_user)


def test_str_and_repr(ops):
    assert str(ops) == "[MailingList ops-team] Operations team - 2 members, 2 sites"
    assert repr(ops).startswith("MailingList(list_id='ops-team'")
