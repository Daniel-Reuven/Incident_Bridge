"""
Incident Bridge backend - automated test suite.

Structure (mirrors the app/ package layout on purpose - see README.md,
section 12):

    tests/
    ├── conftest.py          # shared pytest fixtures (see that file's docstring)
    ├── test_models/         # unit tests for app/models/*
    ├── test_services/       # unit tests for app/services/*
    ├── test_queues/         # unit tests for app/queues/*
    ├── test_*.py            # unit tests for the other app/ modules (iterators,
    │                        #   context managers, sites, scan, report, ...)
    └── test_api/            # integration tests for app/api/* (real HTTP, via TestClient)

Run the whole suite from inside backend/:
    python -m pytest -v

See README.md, section 14, for the full how-to (installing dev
dependencies, coverage reports, and how this plugs into GitHub Actions CI).
"""
