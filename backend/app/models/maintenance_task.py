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

    @classmethod
    def from_dict(cls, data: dict, created_by: User, assigned_to: Optional[User] = None) -> "MaintenanceTask":
        """
        Overrides Incident.from_dict: a maintenance task has no extra
        fields to seed from the record (queue_position is assigned only
        once the task is actually enqueued - see MaintenanceQueue), so
        this just does the shared validation and builds the object. If
        the record supplies an 'id' (the normal case for seed data, so
        re-running the loader is idempotent - see
        IncidentRepository.load_from_jsonl), that id replaces the
        freshly-generated one.
        """
        super().from_dict(data, created_by, assigned_to)
        task = cls(title=data["title"], description=data["description"],
                   created_by=created_by, assigned_to=assigned_to)
        if "id" in data:
            task.id = data["id"]
        return task
