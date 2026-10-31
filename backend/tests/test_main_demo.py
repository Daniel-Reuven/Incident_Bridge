"""
Smoke test for the repository-root demo script, main.py: it must run to the
end without errors, offline, and print every section. Keeps later changes
from silently breaking the demo the brief requires.

Run from backend/:  python -m pytest tests/test_main_demo.py -v
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_main(*args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "main.py"), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_main_py_runs_every_section_cleanly():
    result = run_main()
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    out = result.stdout
    for number in range(1, 16):
        assert f"\n{number}. " in out, f"section {number} missing"
    assert "74 incidents loaded" in out or "incidents loaded" in out
    assert "StopIteration - it is exhausted" in out                  # iterators
    assert "Exhausted - a second loop yields: []" in out              # generator
    assert "Stopped after 2 results" in out                           # lazy pipeline
    assert "Work session interrupted" in out                          # context manager, exception path
    assert "Site health report" in out and "Demo finished." in out    # subsystem report, ran to the end
    # Stage 2 (offline by default): every external-service result is marked SIMULATED, never REAL
    assert "(SIMULATED: no server" in out and "[REAL]" not in out
    assert "[SIMULATED] Answered by: Fake AI" in out                   # the analysis posted its answer
    # the canned answer's findings name the site the fault mentions (seed-079: Site 1007)
    assert "[SIMULATED] Structured findings: {'suggested_severity': 'critical', 'affected_site_ids': (1007,)" in out
    # section 13: findings -> real (scripted) checks -> report metric; an unknown site changes nothing
    assert "Site 1005 (Online store): operational -> operational" in out
    assert "Site 4242: does not exist - an invalid cross-reference" in out
    assert "Site 1005: 1/1/0" in out
    assert "AI-flagged sites now Operational or never notified: [1005]" in out
    # section 14: the same results both ways, in input order; the unknown site fails alone
    assert "Same results: True" in out and "4242: unknown site id" in out
    assert "1001: Operational, 1002: Operational, 1005: Degraded, 1007: Down" in out
    # section 15: three threads, exactly one approved (which one is up to the scheduler)
    assert "Exactly one approval: True" in out and out.count("    refused: tech") == 2
    assert "still in the queue: False" in out
    assert "failed: timed out after 180 seconds." in out               # failure -> error comment
    assert "Fault status, severity and assignee unchanged: True" in out  # ... and no business change
    assert "Factory - build_provider(AIConfig(provider='fake', ...)) creates: FakeProvider" in out


def test_main_py_live_without_a_real_provider_stops_clearly():
    # --live must never fall back to simulated data. Settings set here win over backend/.env
    # (load_dotenv does not override them), so this runs offline even on a machine with a real key.
    env = {**os.environ, "AI_PROVIDER": "gemini", "GEMINI_API_KEY": ""}
    result = run_main("--live", env=env)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "main.py --live: no real AI provider to call" in result.stderr
    assert "GEMINI_API_KEY is not set" in result.stderr
    assert result.stdout == ""                                          # stopped before any demo output

