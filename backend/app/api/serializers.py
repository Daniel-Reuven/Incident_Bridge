"""Converts domain objects (User, Comment, Incident subclasses) into plain dicts for API responses."""

from app.models import Comment, Incident, User


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
    (severity/details for Fault, queue_position for MaintenanceTask), which
    come from the incident itself through the polymorphic kind /
    extra_fields() members (app/models/incident.py). `type` distinguishes
    the two on the frontend without needing a second endpoint shape.
    """
    data = {
        "id": incident.id,
        "type": incident.kind,
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
    data.update(incident.extra_fields())   # each type adds its own fields - no type check here
    return data
