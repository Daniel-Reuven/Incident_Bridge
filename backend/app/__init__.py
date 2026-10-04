"""
Incident Bridge - backend application package.

Subpackages and modules (full file map: README.md, section 12):
    models/     the OOP domain model (incidents, users, comments, sites,
                mailing lists, notifications)
    queues/     the FIFO maintenance queue and the shared fault priority queue
    services/   password policy, severity scoring, site availability checks,
                notification delivery
    api/        the FastAPI application, routers, schemas and serializers
    plus        state, persistence, repositories, iterators/generators,
                context managers, the site directory, incident scan and report
"""
