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
