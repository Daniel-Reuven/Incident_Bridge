"""
Integration tests for the FastAPI HTTP layer in app/api/.

Unlike test_models/test_services/test_queues (pure unit tests with no
HTTP or database involved), these tests exercise the WHOLE stack over
real HTTP requests via FastAPI's TestClient - routing, session auth, the
domain layer, and an isolated in-memory SQLite database all at once. See
tests/conftest.py for the app_client/admin_client/user_client fixtures
every file in here uses.
"""
