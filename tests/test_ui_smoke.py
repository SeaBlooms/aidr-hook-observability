"""Runs the headless dashboard smoke test. The script skips itself when Chrome is absent."""

import subprocess
import sys
from pathlib import Path


def test_dashboard_smoke():
    script = Path(__file__).resolve().parents[1] / "scripts" / "ui_smoke.py"
    result = subprocess.run([sys.executable, str(script)], check=False)
    assert result.returncode == 0
