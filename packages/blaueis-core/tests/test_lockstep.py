"""The three published packages share one version, and the client and the
gateway depend on exactly that core version."""

import tomllib
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]


def _project(name: str) -> dict:
    return tomllib.loads((PACKAGES / name / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_versions_and_core_pins_are_in_lockstep():
    version = _project("blaueis-core")["version"]
    for name in ("blaueis-client", "blaueis-gateway"):
        project = _project(name)
        assert project["version"] == version, f"{name} is {project['version']}, core is {version}"
        core_deps = [d for d in project["dependencies"] if d.replace(" ", "").startswith("blaueis-core")]
        assert core_deps == [f"blaueis-core=={version}"], (
            f"{name} depends on {core_deps}, expected blaueis-core=={version}"
        )
