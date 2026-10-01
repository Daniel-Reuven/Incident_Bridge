"""Comment model: one entry in an incident's update thread."""

import uuid
from datetime import datetime, timezone

from app.models.user import User


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