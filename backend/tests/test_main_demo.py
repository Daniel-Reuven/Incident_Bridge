"""
Smoke test for the repository-root demo script, main.py: it must run to the
end without errors, offline, and print every section. Keeps later changes
from silently breaking the demo the brief requires.

Run from backend/:  python -m pytest tests/test_main_demo.py -v
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_main_py_runs_every_section_cleanly():
    result = subprocess.run([sys.executable, str(ROOT / "main.py")], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    out = result.stdout
    for number in range(1, 10):
        assert f"\n{number}. " in out, f"section {number} missing"
    assert "74 incidents loaded" in out or "incidents loaded" in out
    assert "StopIteration - it is exhausted" in out                  # iterators
    assert "Exhausted - a second loop yields: []" in out              # generator
    assert "Stopped after 2 results" in out                           # lazy pipeline
    assert "Work session interrupted" in out                          # context manager, exception path
    assert "Site health report" in out and "Demo finished." in out    # subsystem report, ran to the end