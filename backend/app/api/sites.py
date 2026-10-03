"""
Site portal endpoints (Sites & Mailing Lists subsystem): sites, mailing
lists, availability checks, manual status changes, notifications, and the
site health report. Mounted at /sites by app/api/app.py.

ADMIN ONLY: every endpoint depends on get_current_admin (app/api/deps.py),
so a regular user gets 403 everywhere - listings included - and an
anonymous caller 401. The domain layer (app/models, app/sites.py) still
checks the admin rule on every change too, exactly like the incident API.

Like app/api/incidents.py, this router stays thin: it calls the
SiteDirectory (state.sites) and lets app/api/app.py's exception handlers
map domain errors to HTTP (ValueError 400, PermissionError 403, KeyError
404, RuntimeError 409 - e.g. "archived", "already being checked", or a
failed notification delivery). Saving is done inside SiteDirectory.

Static paths (/report, /check-all, /mailing-lists..., /notifications...) are
declared BEFORE /{site_id}, so they are never mistaken for a site id.

Endpoints:
    GET    /sites                              list sites (?include_archived=true for all)
    POST   /sites                              create a site
    GET    /sites/report                       site health report
    POST   /sites/check-all                    check every active, non-maintenance site
    GET    /sites/mailing-lists                list mailing lists (?include_archived=true)
    POST   /sites/mailing-lists                create a mailing list
    PATCH  /sites/mailing-lists/{list_id}      edit name / members / related sites
    POST   /sites/mailing-lists/{list_id}/archive | /restore
    GET    /sites/notifications                list notifications, newest first (?state=draft)
    POST   /sites/notifications/{id}/send      send a draft (optionally with edited text)
    POST   /sites/notifications/{id}/dismiss   discard a draft
    GET    /sites/{site_id}                    one site, with history, incidents, notifications
    PATCH  /sites/{site_id}                    edit name / URL / publish date
    POST   /sites/{site_id}/status             manual status change (reason required)
    POST   /sites/{site_id}/check              check one site now
    POST   /sites/{site_id}/archive | /restore

Live updates: every endpoint that changes something publishes one event
through the broadcaster (app/events.py) with roles={"admin"}, so only admin
tabs receive it, and with "scope": "sites", so only the site portal page
reacts to it (frontend/js/events.js). The tab that made the change is
excluded via its X-Client-Id header, exactly like the incident API.
Event shape:
    {"scope": "sites", "action": "<what happened>", "actor": "<username>",
     "subject": "<human-readable thing, e.g. 'Site 1042 (Portal)'>", ...extra}
Actions: site_created, site_updated, site_status_changed, site_checked,
sites_checked, site_archived, site_restored, list_created, list_updated,
list_archived, list_restored, notification_sent, notification_dismissed.
Status-changing events add "status" (the new status) and "notification"
(the state of the notification created for the change - "draft" or
"skipped" - or null when the change was not worth one).
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_broadcaster, get_client_id, get_current_admin, get_state
from app.api.site_schemas import (
    ChangeSiteStatusRequest,
    CreateMailingListRequest,
    CreateSiteRequest,
    DismissNotificationRequest,
    SendNotificationRequest,
    UpdateMailingListRequest,
    UpdateSiteRequest,
)
from app.api.site_serializers import (
    check_outcome_to_dict,
    mailing_list_to_dict,
    notification_to_dict,
    site_detail_to_dict,
    site_to_dict,
    status_outcome_to_dict,
)
from app.events import EventBroadcaster
from app.models import NotificationState, Site, User
from app.reports import build_site_report
from app.state import AppState

router = APIRouter(prefix="/sites", tags=["sites"])

_PORTAL_AUDIENCE = {"admin"}


def _publish(broadcaster: EventBroadcaster, client_id: Optional[str], action: str, actor: User,
             subject: str, **extra) -> None:
    """Publish one site-portal event to every OTHER admin tab (see the module docstring for the shape)."""
    event = {"scope": "sites", "action": action, "actor": actor.username, "subject": subject, **extra}
    broadcaster.publish(event, exclude_client_id=client_id, roles=_PORTAL_AUDIENCE)


def _site_subject(site: Site) -> str:
    return f"{site.label} ({site.site_name})"


def _scan(state: AppState):
    """Run the incident scan once over every incident (the result groups active incidents by site)."""
    return state.sites.scan_incidents(state.incidents.list_all())


# --- sites: collection-level routes (before /{site_id}) ---------------------

@router.get("")
def list_sites(include_archived: bool = False, state: AppState = Depends(get_state),
               _admin: User = Depends(get_current_admin)) -> List[dict]:
    """Every active site (or every site, with ?include_archived=true), in site_id order."""
    scan = _scan(state)
    return [site_to_dict(site, state.sites, scan.incidents_for(site.site_id))
            for site in state.sites.sites(include_archived=include_archived)]


@router.post("")
def create_site(body: CreateSiteRequest, state: AppState = Depends(get_state),
                admin: User = Depends(get_current_admin),
                broadcaster: EventBroadcaster = Depends(get_broadcaster),
                client_id: Optional[str] = Depends(get_client_id)) -> dict:
    site = state.sites.create_site(admin, site_name=body.site_name, site_url=body.site_url,
                                   site_publish_date=body.site_publish_date, site_id=body.site_id)
    _publish(broadcaster, client_id, "site_created", admin, _site_subject(site), site_id=site.site_id)
    return site_to_dict(site, state.sites)


@router.get("/report")
def site_report(state: AppState = Depends(get_state), _admin: User = Depends(get_current_admin)) -> dict:
    """The site health report (app/reports.py) as a dict."""
    return build_site_report(state.sites, state.incidents.list_all()).to_dict()


@router.post("/check-all")
def check_all_sites(state: AppState = Depends(get_state), admin: User = Depends(get_current_admin),
                    broadcaster: EventBroadcaster = Depends(get_broadcaster),
                    client_id: Optional[str] = Depends(get_client_id)) -> dict:
    """Check every active site not in Maintenance, one after another. Can take a few seconds."""
    outcomes = state.sites.check_all()
    changed = sum(1 for o in outcomes if o.change is not None)
    _publish(broadcaster, client_id, "sites_checked", admin, f"{len(outcomes)} sites",
             checked=len(outcomes), changed=changed)
    scan = _scan(state)
    return {"checked": len(outcomes),
            "results": [check_outcome_to_dict(o, state.sites, scan.incidents_for(o.site_id)) for o in outcomes]}


# --- mailing lists -------------------------------------------------------------

@router.get("/mailing-lists")
def list_mailing_lists(include_archived: bool = False, state: AppState = Depends(get_state),
                       _admin: User = Depends(get_current_admin)) -> List[dict]:
    return [mailing_list_to_dict(ml) for ml in state.sites.mailing_lists(include_archived=include_archived)]


@router.post("/mailing-lists")
def create_mailing_list(body: CreateMailingListRequest, state: AppState = Depends(get_state),
                        admin: User = Depends(get_current_admin),
                        broadcaster: EventBroadcaster = Depends(get_broadcaster),
                        client_id: Optional[str] = Depends(get_client_id)) -> dict:
    mailing_list = state.sites.create_mailing_list(admin, list_id=body.list_id, name=body.name,
                                                   members=body.members, site_ids=body.site_ids)
    _publish(broadcaster, client_id, "list_created", admin, f"mailing list {mailing_list.name}",
             list_id=mailing_list.list_id)
    return mailing_list_to_dict(mailing_list)


@router.patch("/mailing-lists/{list_id}")
def update_mailing_list(list_id: str, body: UpdateMailingListRequest, state: AppState = Depends(get_state),
                        admin: User = Depends(get_current_admin),
                        broadcaster: EventBroadcaster = Depends(get_broadcaster),
                        client_id: Optional[str] = Depends(get_client_id)) -> dict:
    mailing_list = state.sites.update_mailing_list(list_id, admin, name=body.name, members=body.members,
                                                   site_ids=body.site_ids)
    _publish(broadcaster, client_id, "list_updated", admin, f"mailing list {mailing_list.name}", list_id=list_id)
    return mailing_list_to_dict(mailing_list)


@router.post("/mailing-lists/{list_id}/archive")
def archive_mailing_list(list_id: str, state: AppState = Depends(get_state),
                         admin: User = Depends(get_current_admin),
                         broadcaster: EventBroadcaster = Depends(get_broadcaster),
                         client_id: Optional[str] = Depends(get_client_id)) -> dict:
    state.sites.archive_mailing_list(list_id, admin)
    mailing_list = state.sites.get_mailing_list(list_id)
    _publish(broadcaster, client_id, "list_archived", admin, f"mailing list {mailing_list.name}", list_id=list_id)
    return mailing_list_to_dict(mailing_list)


@router.post("/mailing-lists/{list_id}/restore")
def restore_mailing_list(list_id: str, state: AppState = Depends(get_state),
                         admin: User = Depends(get_current_admin),
                         broadcaster: EventBroadcaster = Depends(get_broadcaster),
                         client_id: Optional[str] = Depends(get_client_id)) -> dict:
    state.sites.restore_mailing_list(list_id, admin)
    mailing_list = state.sites.get_mailing_list(list_id)
    _publish(broadcaster, client_id, "list_restored", admin, f"mailing list {mailing_list.name}", list_id=list_id)
    return mailing_list_to_dict(mailing_list)


# --- notifications --------------------------------------------------------------

@router.get("/notifications")
def list_notifications(notification_state: Optional[NotificationState] = Query(None, alias="state"),
                       state: AppState = Depends(get_state),
                       _admin: User = Depends(get_current_admin)) -> List[dict]:
    """
    Notifications, newest first. ?state=draft (or sent / dismissed / skipped)
    narrows the list; an unknown value is rejected with 422. (The Python
    parameter is named notification_state only because `state` is already
    the AppState dependency.)
    """
    return [notification_to_dict(n) for n in state.sites.notifications()
            if notification_state is None or n.state == notification_state]


@router.post("/notifications/{notification_id}/send")
def send_notification(notification_id: str, body: SendNotificationRequest, state: AppState = Depends(get_state),
                      admin: User = Depends(get_current_admin),
                      broadcaster: EventBroadcaster = Depends(get_broadcaster),
                      client_id: Optional[str] = Depends(get_client_id)) -> dict:
    notification = state.sites.send_notification(notification_id, admin, message=body.message)
    _publish(broadcaster, client_id, "notification_sent", admin,
             f"the notification about Site {notification.site_id} ({notification.site_name})",
             notification_id=notification.id, site_id=notification.site_id)
    return notification_to_dict(notification)


@router.post("/notifications/{notification_id}/dismiss")
def dismiss_notification(notification_id: str, body: DismissNotificationRequest,
                         state: AppState = Depends(get_state), admin: User = Depends(get_current_admin),
                         broadcaster: EventBroadcaster = Depends(get_broadcaster),
                         client_id: Optional[str] = Depends(get_client_id)) -> dict:
    notification = state.sites.dismiss_notification(notification_id, admin, reason=body.reason)
    _publish(broadcaster, client_id, "notification_dismissed", admin,
             f"the notification about Site {notification.site_id} ({notification.site_name})",
             notification_id=notification.id, site_id=notification.site_id)
    return notification_to_dict(notification)


# --- one site (after the static routes) ------------------------------------------

@router.get("/{site_id}")
def get_site(site_id: int, state: AppState = Depends(get_state), _admin: User = Depends(get_current_admin)) -> dict:
    site = state.sites.get_site(site_id)
    incident_ids = set(_scan(state).incidents_for(site_id))
    incidents = [i for i in state.incidents.list_all() if i.id in incident_ids]
    return site_detail_to_dict(site, state.sites, incidents)


@router.patch("/{site_id}")
def update_site(site_id: int, body: UpdateSiteRequest, state: AppState = Depends(get_state),
                admin: User = Depends(get_current_admin),
                broadcaster: EventBroadcaster = Depends(get_broadcaster),
                client_id: Optional[str] = Depends(get_client_id)) -> dict:
    """Only the fields present in the body are changed; an explicit null publish date clears it."""
    changes = {name: getattr(body, name) for name in body.model_fields_set}
    site = state.sites.update_site(site_id, admin, changes)
    _publish(broadcaster, client_id, "site_updated", admin, _site_subject(site), site_id=site_id)
    return site_to_dict(site, state.sites, _scan(state).incidents_for(site_id))


@router.post("/{site_id}/status")
def change_site_status(site_id: int, body: ChangeSiteStatusRequest, state: AppState = Depends(get_state),
                       admin: User = Depends(get_current_admin),
                       broadcaster: EventBroadcaster = Depends(get_broadcaster),
                       client_id: Optional[str] = Depends(get_client_id)) -> dict:
    outcome = state.sites.change_site_status(site_id, body.status, body.reason, admin)
    if outcome.change is not None:
        _publish(broadcaster, client_id, "site_status_changed", admin, _site_subject(state.sites.get_site(site_id)),
                 site_id=site_id, status=outcome.change.new_status.value,
                 notification=outcome.notification.state.value if outcome.notification else None)
    return status_outcome_to_dict(outcome, state.sites, _scan(state).incidents_for(site_id))


@router.post("/{site_id}/check")
def check_site(site_id: int, state: AppState = Depends(get_state), admin: User = Depends(get_current_admin),
               broadcaster: EventBroadcaster = Depends(get_broadcaster),
               client_id: Optional[str] = Depends(get_client_id)) -> dict:
    outcome = state.sites.check_site(site_id)
    _publish(broadcaster, client_id, "site_checked", admin, _site_subject(state.sites.get_site(site_id)),
             site_id=site_id, status=state.sites.get_site(site_id).status.value,
             changed=outcome.change is not None,
             notification=outcome.notification.state.value if outcome.notification else None)
    return check_outcome_to_dict(outcome, state.sites, _scan(state).incidents_for(site_id))


@router.post("/{site_id}/archive")
def archive_site(site_id: int, state: AppState = Depends(get_state), admin: User = Depends(get_current_admin),
                 broadcaster: EventBroadcaster = Depends(get_broadcaster),
                 client_id: Optional[str] = Depends(get_client_id)) -> dict:
    """Archive the site; the response's `unlinked_list_ids` says which lists it was removed from."""
    unlinked = state.sites.archive_site(site_id, admin)
    _publish(broadcaster, client_id, "site_archived", admin, _site_subject(state.sites.get_site(site_id)),
             site_id=site_id)
    return {"site": site_to_dict(state.sites.get_site(site_id), state.sites), "unlinked_list_ids": unlinked}


@router.post("/{site_id}/restore")
def restore_site(site_id: int, state: AppState = Depends(get_state), admin: User = Depends(get_current_admin),
                 broadcaster: EventBroadcaster = Depends(get_broadcaster),
                 client_id: Optional[str] = Depends(get_client_id)) -> dict:
    """Restore the site; the response's `relinked_list_ids` says which lists it was linked to again."""
    relinked = state.sites.restore_site(site_id, admin)
    _publish(broadcaster, client_id, "site_restored", admin, _site_subject(state.sites.get_site(site_id)),
             site_id=site_id)
    return {"site": site_to_dict(state.sites.get_site(site_id), state.sites, _scan(state).incidents_for(site_id)),
            "relinked_list_ids": relinked}
