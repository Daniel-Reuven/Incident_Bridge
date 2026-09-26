"""
API layer: FastAPI routers, request/response shapes, and dependencies.

This is the HTTP boundary around the domain layer in app/models,
app/queues, and app/services. It deliberately does NOT re-implement
permission checks (admin-only severity changes, admin-only resolution
types, current-password checks) - those stay enforced exactly once,
inside the domain layer, and this layer just translates the resulting
PermissionError / ValueError / RuntimeError / KeyError / IndexError into
the right HTTP status code (see app/api/app.py's exception handlers).
"""
