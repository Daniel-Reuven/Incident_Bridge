"""
Converts Sites & Mailing Lists objects into plain dicts for API responses
(app/api/sites.py). Kept apart from app/api/serializers.py (incidents) so the
subsystem stays self-contained.

Enum values are sent as their string value ("down", "draft"), datetimes and
dates as ISO 8601 strings, sets as sorted lists - so every response is
stable and directly JSON-serializable.
"""

from typing import Iterable, List, Optional

from app.context import SiteCheckSession
from app.models import AvailabilityCheck, Incident, MailingList, Notification, Site, SiteStatusChange
from app.sites import SiteCheckOutcome, SiteDirectory, StatusChangeOutcome


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def check_to_dict(check: Optional[AvailabilityCheck]) -> Optional[dict]:
    if check is None:
        return None
    return {"checked_at": _iso(check.checked_at), "observed_status": check.observed_status.value,
            "http_status": check.http_status, "response_ms": check.response_ms, "error": check.error,
            "summary": check.summary}


def change_to_dict(change: Optional[SiteStatusChange]) -> Optional[dict]:
    if change is None:
        return None
    return {"old_status": change.old_status.value, "new_status": change.new_status.value,
            "reason": change.reason, "actor": change.actor, "changed_at": _iso(change.changed_at)}


def site_to_dict(site: Site, directory: SiteDirectory, incident_ids: Iterable[str] = ()) -> dict:
    """
    A site for the list view: its own fields, its latest check, the ACTIVE
    lists covering it, the ids of the active incidents mentioning it (from
    the incident scan - passed in, so a listing scans once for all sites),
    and whether a check of it is running right now.
    """
    return {
        "site_id": site.site_id,
        "label": site.label,
        "site_name": site.site_name,
        "site_url": site.site_url,
        "site_publish_date": _iso(site.site_publish_date),
        "status": site.status.value,
        "created_at": _iso(site.created_at),
        "is_archived": site.is_archived,
        "archived_at": _iso(site.archived_at),
        "last_check": check_to_dict(site.last_check),
        "consecutive_failures": site.consecutive_failures,
        "checking": SiteCheckSession.is_running(site.site_id),
        "list_ids": [ml.list_id for ml in directory.lists_covering(site.site_id)],
        "incident_ids": sorted(incident_ids),
    }


def incident_summary(incident: Incident) -> dict:
    """The few incident fields the site portal shows - enough to link to the incident page."""
    return {"id": incident.id, "title": incident.title, "status": incident.status.value,
            "type": incident.kind}


def site_detail_to_dict(site: Site, directory: SiteDirectory, incidents: List[Incident]) -> dict:
    """site_to_dict plus the full status history, the mentioning incidents, and this site's notifications."""
    data = site_to_dict(site, directory, (i.id for i in incidents))
    data["status_history"] = [change_to_dict(c) for c in site.status_history]
    data["archived_list_ids"] = sorted(site.archived_list_ids)
    data["incidents"] = [incident_summary(i) for i in incidents]
    data["notifications"] = [notification_to_dict(n) for n in directory.notifications()
                             if n.site_id == site.site_id]
    return data


def mailing_list_to_dict(mailing_list: MailingList) -> dict:
    return {
        "list_id": mailing_list.list_id,
        "name": mailing_list.name,
        "members": list(mailing_list.members),
        "member_count": len(mailing_list),
        "site_ids": list(mailing_list.site_ids),
        "is_archived": mailing_list.is_archived,
        "archived_at": _iso(mailing_list.archived_at),
        "created_at": _iso(mailing_list.created_at),
    }


def notification_to_dict(notification: Notification) -> dict:
    return {
        "id": notification.id,
        "site_id": notification.site_id,
        "site_name": notification.site_name,
        "old_status": notification.old_status.value,
        "new_status": notification.new_status.value,
        "list_ids": list(notification.list_ids),
        "recipients": sorted(notification.recipients),
        "recipient_count": len(notification.recipients),
        "message": notification.message,
        "state": notification.state.value,
        "created_by": notification.created_by,
        "created_at": _iso(notification.created_at),
        "decided_by": notification.decided_by,
        "decided_at": _iso(notification.decided_at),
        "dismiss_reason": notification.dismiss_reason,
    }


def check_outcome_to_dict(outcome: SiteCheckOutcome, directory: SiteDirectory,
                          incident_ids: Iterable[str] = ()) -> dict:
    """One check's result: the updated site, the check, the status change and the drafted notification (or None)."""
    return {
        "site": site_to_dict(directory.get_site(outcome.site_id), directory, incident_ids),
        "check": check_to_dict(outcome.check),
        "change": change_to_dict(outcome.change),
        "notification": notification_to_dict(outcome.notification) if outcome.notification else None,
        "duration_ms": round(outcome.duration_ms, 1),
    }


def status_outcome_to_dict(outcome: StatusChangeOutcome, directory: SiteDirectory,
                           incident_ids: Iterable[str] = ()) -> dict:
    """A manual status change's result: the updated site, the change (None if unchanged) and its notification."""
    return {
        "site": site_to_dict(directory.get_site(outcome.site_id), directory, incident_ids),
        "change": change_to_dict(outcome.change),
        "notification": notification_to_dict(outcome.notification) if outcome.notification else None,
    }
