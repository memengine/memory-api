"""Run all three steps with mock mode explicitly enabled."""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

for script in ("billing_says_growth.py", "crm_says_scale.py", "ask_agent.py"):
    subprocess.run(
        [sys.executable, str(HERE / script)],
        env={**os.environ, "MEMORYOS_DEMO_MOCK": "1"},
        check=True,
    )
