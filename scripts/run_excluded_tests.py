#!/usr/bin/env python3
"""Run every script-style test excluded from pytest collection.

blaueis-core's legacy tests execute on import and call sys.exit(), so
pytest can't collect them (tests/conftest.py ``collect_ignore``). They
still assert real protocol behaviour — this runner executes each one and
aggregates exit codes so CI keeps them green instead of letting them rot.

The script list is read from conftest.py's ``collect_ignore``: a script
added there is automatically picked up here. Scripts that need fixture
arguments are mapped in ``ARGV`` (test_pipeline runs once per pipeline).

Usage (from the repo root):
    python3 scripts/run_excluded_tests.py
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "packages" / "blaueis-core" / "tests"

# Fixture argv per script, relative to the tests directory. Every other
# script runs bare. A script listed with N argv sets runs N times.
ARGV: dict[str, list[list[str]]] = {
    "test_command_builder.py": [
        ["test-cases/command_builder/command_tests.yaml"],
    ],
    "test_process_frame.py": [
        ["test-cases/process_frame_tests/process_tests.yaml"],
    ],
    "test_pipeline.py": [
        ["test-cases/pipeline_cool_only/pipeline.yaml"],
        ["test-cases/pipeline_xtremesaveblue/pipeline.yaml"],
    ],
}


def collect_ignore_list() -> list[str]:
    tree = ast.parse((TESTS / "conftest.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == "collect_ignore":
                    return [ast.literal_eval(e) for e in node.value.elts]
    raise SystemExit("collect_ignore not found in tests/conftest.py")


def main() -> int:
    failures: list[str] = []
    for name in collect_ignore_list():
        for args in ARGV.get(name, [[]]):
            label = name if not args else f"{name} [{Path(args[-1]).parent.name}]"
            proc = subprocess.run(
                [sys.executable, str(TESTS / name), *(str(TESTS / a) for a in args)],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                print(f"  ok    {label}")
            else:
                print(f"  FAIL  {label} (exit {proc.returncode})")
                tail = (proc.stdout + proc.stderr).splitlines()[-25:]
                print("\n".join(f"        {line}" for line in tail))
                failures.append(label)
    if failures:
        print(f"\n{len(failures)} excluded script(s) failing: {', '.join(failures)}")
        return 1
    print("\nall excluded scripts green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
