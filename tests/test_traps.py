"""함정 회귀 테스트(예시 TC)를 가짜 LLM·가짜 액션 API로 돌려 모두 통과하는지 본다. LLM·네트워크 불필요."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_example_traps_all_pass():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, str(ROOT / "run_traps.py")], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr[-2000:]
