"""MaintenanceTask: the maintenance-incident subclass."""

from typing import Optional

from app.models.incident import Incident
from app.models.user import User


class MaintenanceTask(Incident):
    """
    A routine maintenance incident. FIFO ordering is enforced entirely by
    MaintenanceQueue (see app/queues/maintenance_queue.py) - this class
    only tracks the position it was last assigned, for display purposes.
    It is not used for ordering logic itself.
    """

    def __init__(self, title: str, description: str, created_by: User,
                 assigned_to: Optional[User] = None):
        super().__init__(title, description, created_by, assigned_to)
        self.queue_position: Optional[int] = None

    def __str__(self) -> str:
        position = f"Pos {self.queue_position}" if self.queue_position is not None else "Unqueued"
        return f"[MaintenanceTask - {position}] {self.title}"

    @classmethod
    def from_dict(cls, data: dict, created_by: User, assigned_to: Optional[User] = None) -> "MaintenanceTask":
        """
        Overrides Incident.from_dict: a maintenance task has no extra
        fields to seed from the record (queue_position is assigned only
        once the task is actually enqueued - see MaintenanceQueue), so
        this just does the shared validation (including the required
        'id') and builds the object. The record's id replaces the
        freshly-generated one, which is what makes re-running the loader
        idempotent - see IncidentRepository.load_from_jsonl.
        """
        super().from_dict(data, created_by, assigned_to)
        task = cls(title=data["title"], description=data["description"],
                   created_by=created_by, assigned_to=assigned_to)
        task.id = cls.incident_id_from(data)
        return task