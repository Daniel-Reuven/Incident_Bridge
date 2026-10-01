"""
Unit tests for app.context.IncidentWorkSession - the context manager that
guarantees an incident's audit trail (its comment thread) reflects what
happened during a block of work, whether that block succeeds or raises.
"""

import pytest

from app.context import IncidentWorkSession
from app.models.enums import IncidentStatus, Role
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User


@pytest.fixture
def actor():
    return User(username="tech", role=Role.USER, password="Passw0rd1")


@pytest.fixture
def task(actor):
    return MaintenanceTask(title="Patch server", description="desc", created_by=actor)


def test_enter_moves_an_open_task_to_in_progress(task, actor):
    with IncidentWorkSession(task, actor=actor):
        assert task.status == IncidentStatus.IN_PROGRESS


def test_a_clean_exit_adds_a_completed_comment(task, actor):
    with IncidentWorkSession(task, actor=actor):
        pass  # simulate successful work - nothing raised
    assert len(task.comments) == 1
    assert "completed" in task.comments[-1].text.lower()


def test_an_exception_inside_the_block_is_never_swallowed(task, actor):
    """
    __exit__ returns False, meaning it must NEVER suppress an exception -
    the caller's own code should see exactly the same error it would have
    gotten without this context manager wrapping it.
    """
    with pytest.raises(ValueError, match="something went wrong"):
        with IncidentWorkSession(task, actor=actor):
            raise ValueError("something went wrong")


def test_an_exception_inside_the_block_still_adds_an_interrupted_comment(task, actor):
    try:
        with IncidentWorkSession(task, actor=actor):
            raise ValueError("boom")
    except ValueError:
        pass  # expected - the exception itself is covered by the test above; here we only check the comment

    assert len(task.comments) == 1
    comment_text = task.comments[-1].text.lower()
    assert "interrupted" in comment_text
    assert "valueerror" in comment_text

def test_str_representation_pending(task, actor):
    session = IncidentWorkSession(task, actor=actor)
    expected_str = f"IncidentWorkSession(incident={task}, actor={actor}, state=Pending)"
    assert str(session) == expected_str


def test_str_representation_active(task, actor):
    session = IncidentWorkSession(task, actor=actor)
    with session:
        expected_str = f"IncidentWorkSession(incident={task}, actor={actor}, state=Active)"
        assert str(session) == expected_str