"""Render the walkthrough when VHS is available."""

import shutil
import subprocess

if shutil.which("vhs"):
    subprocess.run(["vhs", "docs/assets/demo.tape"], check=True)
else:
    print("Install VHS, ttyd and FFmpeg: https://github.com/charmbracelet/vhs#installation")
