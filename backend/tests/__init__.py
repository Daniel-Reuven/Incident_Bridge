"""
Incident Bridge backend - automated test suite (Stage 3).

Structure (mirrors the app/ package layout on purpose - see
backend/README.md's "What's included" section):

    tests/
    ├── conftest.py          # shared pytest fixtures (see that file's docstring)
    ├── test_models/         # unit tests for app/models/*
    ├── test_services/       # unit tests for app/services/*
    ├── test_queues/         # unit tests for app/queues/*
    ├── test_context.py      # unit tests for app/context.py
    └── test_api/            # integration tests for app/api/* (real HTTP, via TestClient)

Run the whole suite from inside backend/:
    python -m pytest -v

See backend/README.md for the full how-to (installing dev dependencies,
running a single file, coverage reports, and how this plugs into GitHub
Actions CI).
"""
