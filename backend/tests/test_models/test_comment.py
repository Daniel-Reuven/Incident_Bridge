"""Unit tests for app.models.comment.Comment."""

import pytest

from app.models.comment import Comment
from app.models.enums import Role
from app.models.user import User


@pytest.fixture
def author():
    return User(username="alice", role=Role.USER, password="Passw0rd1")


def test_comment_stores_stripped_text(author):
    comment = Comment(author=author, text="  hello there  ")
    assert comment.text == "hello there"


def test_comment_has_an_id_and_a_timestamp(author):
    comment = Comment(author=author, text="hello")
    assert comment.id
    assert comment.created_at is not None


def test_empty_text_is_rejected(author):
    with pytest.raises(ValueError):
        Comment(author=author, text="")


def test_whitespace_only_text_is_rejected(author):
    with pytest.raises(ValueError):
        Comment(author=author, text="   ")
