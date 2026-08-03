"""Smoke test for the ``blaueis-collisions`` CLI.

Guards two things the library tests do not: that the CLI resolves the bundled
glossary and the allowlist that ships next to it as package data, and that the
current glossary is clean under that allowlist (``--ci`` exits 0). If the
allowlist stops shipping — e.g. a package-data glob regresses — ``load_allowlist``
would see no entries and the known ``rsp_0xc0`` body[9] bit-4 overlap would fail
``--ci``, turning this green→red.
"""

from __future__ import annotations

from blaueis.tools import check_collisions


def test_allowlist_ships_next_to_glossary():
    assert check_collisions.ALLOWLIST.name == "glossary_collisions.allow.yaml"
    assert check_collisions.ALLOWLIST.parent == check_collisions.GLOSSARY.parent
    assert check_collisions.ALLOWLIST.is_file()


def test_ci_run_is_clean():
    assert check_collisions.main(["--ci"]) == 0


def test_no_allow_surfaces_the_known_overlap():
    # Without the allowlist the structural rsp_0xc0 overlap must reappear.
    assert check_collisions.main(["--ci", "--no-allow"]) == 1
