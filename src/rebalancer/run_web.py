"""Entry point to launch the Streamlit web app."""

import subprocess
import sys
from pathlib import Path


def main():
    web_py = Path(__file__).parent / "web.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(web_py), "--server.headless=true"])
