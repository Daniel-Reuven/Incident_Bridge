"""
Tests for the text-length limits added in AI analysis feature step 1:

- MAX_COMMENT_LENGTH (app/models/comment.py), enforced in
  Incident.add_comment() for every new comment;
- MAX_REASON_LENGTH (app/models/incident.py), enforced up front in
  Incident.close() and Incident.reopen() so a status change can never
  succeed and then fail while writing its log comment.

Run from backend/:  python -m pytest tests/test_models/test_comment_limits.py -v
"""

import pytest

from app.models import Comment, Fault, IncidentStatus, ResolutionType, SeverityCategory
from app.models.comment import MAX_COMMENT_LENGTH
from app.models.incident import MAX_REASON_LENGTH


@pytest.fixture
def fault(regular_user):
    return Fault(title="Disk full", description="The log volume is full.", created_by=regular_user,
                 severity=SeverityCategory.MAJOR, severity_score=50.0)


# --- comments -------------------------------------------------------------------

def test_a_comment_at_the_limit_is_accepted(fault, regular_user):
    fault.add_comment(regular_user, "x" * MAX_COMMENT_LENGTH)
    assert len(fault.comments[-1].text) == MAX_COMMENT_LENGTH


def test_a_comment_over_the_limit_is_rejected_and_not_added(fault, regular_user):
    with pytest.raises(ValueError, match=f"at most {MAX_COMMENT_LENGTH} characters"):
        fault.add_comment(regular_user, "x" * (MAX_COMMENT_LENGTH + 1))
    assert fault.comments == []


def test_surrounding_whitespace_does_not_count(fault, regular_user):
    fault.add_comment(regular_user, "   " + "x" * MAX_COMMENT_LENGTH + "   ")
    assert len(fault.comments[-1].text) == MAX_COMMENT_LENGTH


def test_restoring_an_old_long_comment_still_works(regular_user):
    """Comment(...) itself has no maximum, so a comment saved before the limit existed still loads."""
    comment = Comment(author=regular_user, text="x" * (MAX_COMMENT_LENGTH + 100))
    assert len(comment.text) == MAX_COMMENT_LENGTH + 100


# --- closing messages and reopen reasons -------------------------------------------

def test_a_closing_message_at_the_limit_is_accepted(fault, regular_user):
    fault.close(regular_user, ResolutionType.RESOLVED, "m" * MAX_REASON_LENGTH)
    assert fault.status == IncidentStatus.CLOSED


def test_a_too_long_closing_message_changes_nothing(fault, regular_user):
    with pytest.raises(ValueError, match=f"at most {MAX_REASON_LENGTH} characters"):
        fault.close(regular_user, ResolutionType.RESOLVED, "m" * (MAX_REASON_LENGTH + 1))
    assert fault.status == IncidentStatus.OPEN
    assert fault.comments == []


def test_a_too_long_reopen_reason_changes_nothing(fault, admin_user):
    fault.close(admin_user, ResolutionType.RESOLVED, "Fixed.")
    comments_before = len(fault.comments)
    with pytest.raises(ValueError, match=f"at most {MAX_REASON_LENGTH} characters"):
        fault.reopen(admin_user, IncidentStatus.OPEN, "r" * (MAX_REASON_LENGTH + 1))
    assert fault.status == IncidentStatus.CLOSED
    assert len(fault.comments) == comments_before


def test_longest_reopen_log_comment_still_fits(fault, admin_user):
    """Worst case: a maximum-length closing message AND a maximum-length reopen reason in one log comment."""
    fault.close(admin_user, ResolutionType.RESOLVED, "m" * MAX_REASON_LENGTH)
    fault.reopen(admin_user, IncidentStatus.IN_PROGRESS, "r" * MAX_REASON_LENGTH)
    assert len(fault.comments[-1].text) <= MAX_COMMENT_LENGTH
