"""Comment model: one entry in an incident's update thread."""

import uuid
from datetime import datetime, timezone

from app.models.user import User

# The longest a NEW comment may be, in characters (after trimming). Applies
# to everyone: comments typed by users, automatic status-change log
# comments, and comments posted by the built-in "ai-assistant" system user (which
# splits longer AI answers into several comments).
#
# It is enforced in Incident.add_comment() - the single entry point for new
# comments - rather than in Comment.__init__ below, on purpose: comments
# restored from the database (app/persistence.py) are rebuilt with
# Comment(...) directly, so a comment saved before this limit existed still
# loads instead of stopping the app from starting.
#
# The frontend mirrors this value in frontend/js/text-limits.js - change
# both together.
MAX_COMMENT_LENGTH = 5000


class Comment:
    """A single comment/update on an Incident, written by any authenticated user."""

    def __init__(self, author: User, text: str):
        if not text or not text.strip():
            raise ValueError("Comment text cannot be empty.")
        self.id = str(uuid.uuid4())
        self.author = author
        self.text = text.strip()
        self.created_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        preview = self.text if len(self.text) <= 30 else self.text[:27] + "..."
        return f"Comment(author={self.author.username!r}, text={preview!r})"

    def __str__(self) -> str:
        formatted_time = self.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"[{formatted_time}] {self.author.username}: {self.text}"
