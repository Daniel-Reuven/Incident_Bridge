"""
AI analysis of faults (the "Analyze using <provider>" button on a fault page).

One AI provider at a time - Gemini (default), Claude or OpenAI - chosen in
backend/.env (see config.py). A single named worker thread (worker.py) takes
analysis requests from a persisted FIFO queue (job_store.py), builds the
prompt from the fault and all its comments (prompt.py), calls the provider
(providers/), and posts the answer back as comments (splitter.py) by the
built-in "ai-assistant" user (app/models/system_users.py).

Files:
    config.py       AIConfig: reads and validates the AI_* / <PROVIDER>_* settings
    providers/      AnalysisProvider interface + model fallback (base.py), one file per
                    provider, the registry, and Gemini's tier model lists (gemini_models.py)
    prompt.py       instructions + incident JSON sent to the model
    splitter.py     answer -> comment texts (header, parts, truncation, errors)
    jobs.py         AnalysisJob
    job_store.py    SqliteAnalysisJobStore: the queue in SQLite
    worker.py       AnalysisWorker: the queue + the one worker thread
    setup.py        check_ai_settings(), create_analysis_worker() - used by serve_app.py / app.py

The HTTP endpoints are in app/api/analysis.py; the page side is in
frontend/js/incident.js.
"""
