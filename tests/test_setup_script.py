"""setup.ps1 must at least parse (a syntax error would only show up on a fresh PC)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parent.parent / "setup.ps1"


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"), reason="Windows PowerShell only")
def test_setup_script_parses():
    command = ("$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
               f"'{SETUP}', [ref]$null, [ref]$e) | Out-Null; if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_setup_script_never_deletes_anything_but_the_venv():
    text = SETUP.read_text(encoding="utf-8")
    deletes = [line.strip() for line in text.splitlines() if "Remove-Item" in line]
    assert deletes == ["Remove-Item -Recurse -Force $Venv"]
    assert "Refusing to delete" in text  # and it checks that path first
