"""
Most files here are scripts (`python3 -m tests.test_x`) that assert at the top
level instead of defining `test_` functions. Collect each of those as a single
pytest item that runs the script in a subprocess, so `pytest` covers them all
without importing them (importing would run them, and their env setup would
leak into the other modules).
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Manual tools, not tests: test_synthesis calls the configured LLM on today's
# real sessions; test_search prints matches for a query given on the command line.
collect_ignore = ["test_search.py", "test_synthesis.py"]


def pytest_pycollect_makemodule(module_path, parent):
    if "\ndef test_" not in module_path.read_text():
        return ScriptFile.from_parent(parent, path=module_path)


class ScriptFile(pytest.File):
    def collect(self):
        yield ScriptItem.from_parent(self, name=self.path.stem)


class ScriptItem(pytest.Item):
    def runtest(self):
        proc = subprocess.run(
            [sys.executable, "-m", f"tests.{self.path.stem}"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise AssertionError(f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}")

    def reportinfo(self):
        return self.path, None, f"python -m tests.{self.path.stem}"
