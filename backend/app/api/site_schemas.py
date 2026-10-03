"""
Pydantic request bodies for the site portal API (app/api/sites.py).

Kept apart from app/api/schemas.py (the incident API's bodies) so the Sites
& Mailing Lists subsystem stays a self-contained module.

These bodies only describe the SHAPE of a request (which fields, which JSON
types). The business rules - valid URL, publish date not in the future,
valid email, unique ids - are checked by the domain layer (app/models,
app/sites.py), which raises ValueError; app/api/app.py turns that into HTTP
400 with the domain's own message. That keeps every rule in one place.

As in schemas.py, response bodies are plain dicts (app/api/site_serializers.py).
"""

from typing import List, Optional

from pydantic import BaseModel

from app.models.enums import SiteStatus


class CreateSiteRequest(BaseModel):
    """POST /sites. `site_id` is optional: without it the next free id is used."""
    site_name: str
    site_url: str
    site_publish_date: Optional[str] = None   # "YYYY-MM-DD"
    site_id: Optional[int] = None


class UpdateSiteRequest(BaseModel):
    """
    PATCH /sites/{site_id}. Send only the fields to change. Sending
    "site_publish_date": null clears the date; leaving it out keeps it.
    (The router tells the two apart with model_fields_set.)
    """
    site_name: Optional[str] = None
    site_url: Optional[str] = None
    site_publish_date: Optional[str] = None


class ChangeSiteStatusRequest(BaseModel):
    """POST /sites/{site_id}/status - a manual change with a required reason."""
    status: SiteStatus
    reason: str


class CreateMailingListRequest(BaseModel):
    """POST /sites/mailing-lists."""
    list_id: str
    name: str
    members: List[str] = []
    site_ids: List[int] = []


class UpdateMailingListRequest(BaseModel):
    """
    PATCH /sites/mailing-lists/{list_id}. Fields left out are not changed;
    `members` and `site_ids` REPLACE the current sets when given.
    """
    name: Optional[str] = None
    members: Optional[List[str]] = None
    site_ids: Optional[List[int]] = None


class SendNotificationRequest(BaseModel):
    """POST /sites/notifications/{id}/send. `message` optionally replaces the draft's text before sending."""
    message: Optional[str] = None


class DismissNotificationRequest(BaseModel):
    """POST /sites/notifications/{id}/dismiss. The reason is optional."""
    reason: Optional[str] = None
