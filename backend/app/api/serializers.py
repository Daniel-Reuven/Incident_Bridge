"""Converts domain objects (User, Comment, Incident subclasses) into plain dicts for API responses."""

from app.models import Comment, Fault, Incident, User


def user_to_dict(user: User) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role.value}


def comment_to_dict(comment: Comment) -> dict:
    return {
        "id": comment.id,
        "author": comment.author.username,
        "text": comment.text,
        "created_at": comment.created_at.isoformat(),
    }


def incident_to_dict(incident: Incident) -> dict:
    """
    Shared fields for both incident types, plus type-specific fields
    (severity/details for Fault, queue_position for MaintenanceTask).
    `type` distinguishes the two on the frontend without needing a second
    endpoint shape.
    """
    is_fault = isinstance(incident, Fault)
    data = {
        "id": incident.id,
        "type": "fault" if is_fault else "maintenance",
        "title": incident.title,
        "description": incident.description,
        "status": incident.status.value,
        "resolution_type": incident.resolution_type.value if incident.resolution_type else None,
        "resolution_message": incident.resolution_message,
        "created_by": incident.created_by.username,
        "assigned_to": incident.assigned_to.username if incident.assigned_to else None,
        "created_at": incident.created_at.isoformat(),
        "updated_at": incident.updated_at.isoformat(),
        "comments": [comment_to_dict(c) for c in incident.comments],
    }
    if is_fault:
        data["severity"] = incident.severity.value
        data["severity_score"] = incident.severity_score
        data["details"] = incident.details
    else:
        data["queue_position"] = incident.queue_position
    return data
