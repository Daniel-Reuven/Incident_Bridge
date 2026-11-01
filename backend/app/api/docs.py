"""
FastAPI's API documentation - /docs (Swagger UI), /redoc and the schema
they read, /openapi.json - behind the API_DOCS_ACCESS setting (Server
settings > Settings > Server, app/app_config.py):

    login   (default) only for someone logged in: the pages send anyone else
            to the login page, /openapi.json answers 401
    public  open to anyone, like FastAPI's default

The documentation only describes the API - every endpoint still checks the
login and the role itself - but by default it is not handed to strangers.
Read from the environment on each request; the saved setting reaches the
environment at the next start (app_config.apply()), like every setting.
"""

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.deps import get_current_user

API_DOCS_ENV = "API_DOCS_ACCESS"
CHOICES = ("login", "public")
OPENAPI_URL = "/openapi.json"

router = APIRouter(include_in_schema=False)


def docs_access() -> str:
    """API_DOCS_ACCESS: login (default) or public. ValueError for anything else (startup stops with it)."""
    value = (os.environ.get(API_DOCS_ENV) or "login").strip().lower()
    if value not in CHOICES:
        raise ValueError(f"{API_DOCS_ENV} must be login or public (got {value!r}).")
    return value


def _allowed(request: Request) -> bool:
    if docs_access() == "public":
        return True
    try:
        get_current_user(request)
    except HTTPException:
        return False
    return True


@router.get(OPENAPI_URL)
def openapi_schema(request: Request):
    if not _allowed(request):
        raise HTTPException(status_code=401, detail="Log in to read the API documentation.")
    return JSONResponse(request.app.openapi())


@router.get("/docs")
def swagger_ui(request: Request):
    if not _allowed(request):
        return RedirectResponse("/", status_code=303)
    return get_swagger_ui_html(openapi_url=OPENAPI_URL, title=f"{request.app.title} - docs",
                               oauth2_redirect_url="/docs/oauth2-redirect")


@router.get("/docs/oauth2-redirect")
def swagger_ui_redirect():
    return get_swagger_ui_oauth2_redirect_html()


@router.get("/redoc")
def redoc(request: Request):
    if not _allowed(request):
        return RedirectResponse("/", status_code=303)
    return get_redoc_html(openapi_url=OPENAPI_URL, title=f"{request.app.title} - ReDoc")
