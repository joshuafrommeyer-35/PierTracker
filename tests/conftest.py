"""Test setup: the tracker and community modules are plain scripts in their folders."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in ("tracker", "community"):
    sys.path.insert(0, str(ROOT / folder))
