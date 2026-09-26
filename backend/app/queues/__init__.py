"""Queue package: the FIFO maintenance queue and the shared fault priority queue."""

from app.queues.maintenance_queue import MaintenanceQueue, MaintenanceQueueManager
from app.queues.fault_queue import FaultPriorityQueue

__all__ = ["MaintenanceQueue", "MaintenanceQueueManager", "FaultPriorityQueue"]
