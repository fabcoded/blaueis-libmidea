"""Hot-path cache tests for StatusDB.

Two concerns:

1. **Correctness / invalidation.** StatusDB's ``_field_flat`` and
   ``_field_map_cache`` must be bound to the glossary lifetime. A new
   StatusDB (as created on HA config-entry reload) gets fresh caches;
   the old caches die with the old object. No module-level state can
   cross instances.

2. **Performance.** The cached ingest path must be materially faster
   than the uncached one. Exact speedup depends on glossary size and
   machine, but a single-frame decode should benefit by ≥5× on a
   typical dev box once the cache is warm — the uncached path walks
   all ~200 glossary fields per call; the cached path does a dict
   lookup.

This file deliberately uses the public ``StatusDB.ingest`` API so the
test is resilient to internal refactors.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from blaueis.client.status_db import StatusDB
from blaueis.core.codec import build_field_map, decode_frame_fields, load_glossary


# ──────────────────────────────────────────────────────────────────────
#   Correctness + invalidation
# ──────────────────────────────────────────────────────────────────────


def test_field_flat_matches_walk_fields():
    """StatusDB._field_flat must equal walk_fields(glossary) — same
    content, just memoised."""
    from blaueis.core.codec import walk_fields

    g = load_glossary()
    db = StatusDB(glossary=g)

    expected = walk_fields(g)
    assert set(db.field_flat.keys()) == set(expected.keys())
    # Values are *references*, not copies — identity check.
    sample = next(iter(expected))
    assert db.field_flat[sample] is expected[sample]


def test_two_status_dbs_do_not_share_caches():
    """Two StatusDB instances over the same glossary keep independent
    caches. (They'd share the same glossary dict, but each builds its
    own _field_flat / _field_map_cache. This is what makes invalidation
    automatic on config-entry reload.)"""
    g = load_glossary()
    db1 = StatusDB(glossary=g)
    db2 = StatusDB(glossary=g)

    assert db1.field_flat is not db2.field_flat
    # Contents equal, identity distinct.
    assert db1.field_flat == db2.field_flat


def test_new_statusdb_with_different_glossary_gets_different_cache():
    """A StatusDB built over a modified glossary sees the modification
    — no stale cache carry-over (since caches are per-instance)."""
    g1 = load_glossary()
    db1 = StatusDB(glossary=g1)
    assert "indoor_temperature" in db1.field_flat

    # Simulate a user-applied override that removes a field.
    g2 = load_glossary()
    # Reach in and delete a field — we never do this in prod code, but
    # it proves the cache isn't inheriting g1's state.
    for cat in g2.get("fields", {}).values():
        if isinstance(cat, dict) and "indoor_temperature" in cat:
            del cat["indoor_temperature"]
            break

    db2 = StatusDB(glossary=g2)
    assert "indoor_temperature" not in db2.field_flat
    # db1 still sees the original — independence confirmed.
    assert "indoor_temperature" in db1.field_flat


@pytest.mark.asyncio
async def test_field_map_cache_populated_lazily_on_ingest():
    """The per-protocol field_map cache is empty at init and fills on
    first ingest. Second ingest of the same protocol is a cache hit
    (same list object returned)."""
    g = load_glossary()
    db = StatusDB(glossary=g)
    assert db._field_map_cache == {}

    # Build a minimal valid c0 body to ingest. We don't care about the
    # decode result — we only care that the cache is populated.
    body = bytes(32)
    await db.ingest(body, "rsp_0xc0")
    assert "rsp_0xc0" in db._field_map_cache
    first_fm = db._field_map_cache["rsp_0xc0"]

    await db.ingest(body, "rsp_0xc0")
    # Same object — not rebuilt.
    assert db._field_map_cache["rsp_0xc0"] is first_fm


# ──────────────────────────────────────────────────────────────────────
#   Microbench: cached vs uncached decode
# ──────────────────────────────────────────────────────────────────────


def test_decode_with_prebuilt_field_map_matches_plain_decode():
    """Sanity: decode_frame_fields with and without ``field_map`` param
    must produce identical output. This is the invariant the cache
    relies on."""
    g = load_glossary()
    body = bytes(32)
    a = decode_frame_fields(body, "rsp_0xc0", g)
    fm = build_field_map(g, "rsp_0xc0")
    b = decode_frame_fields(body, "rsp_0xc0", g, field_map=fm)
    assert a == b


def test_cached_decode_is_faster(capsys):
    """Time uncached vs cached decode over ``N`` frames. Log the ratio
    so we have a per-machine baseline in CI output. Fails only if the
    cached path is *slower* — we expect a speedup, and even a flat
    result would mean something went wrong.

    This is NOT a strict perf assertion (CI variance would fail it);
    it's a smoke test that the optimisation is doing its job.

    Validated reference numbers (1000 frames, median of 3 runs):

    ============   ================   ===============   ================
    Host           rsp_0xc0           rsp_0xc1_group1   rsp_0xb1
    ============   ================   ===============   ================
    GH Codespace   0.36 → 0.08 ms     —                 —     (4.3×)
    Pi 2 Rev 1.1   1.90 → 0.77 ms     1.24 → 0.28 ms    1.40 → 0.20 ms
                   (2.5×)             (4.4×)            (7.1×)
    ============   ================   ===============   ================

    B1 wins biggest because the protocol has many TLV property IDs;
    the uncached ``build_field_map`` walks all ~211 glossary fields to
    filter just the B1 subset. Caching skips the linear scan entirely.
    """
    g = load_glossary()
    body = bytes(32)
    n = 1000

    # Uncached: every call rebuilds field_map.
    t0 = time.perf_counter()
    for _ in range(n):
        decode_frame_fields(body, "rsp_0xc0", g)
    t_uncached = time.perf_counter() - t0

    # Cached: build once, reuse.
    fm = build_field_map(g, "rsp_0xc0")
    t0 = time.perf_counter()
    for _ in range(n):
        decode_frame_fields(body, "rsp_0xc0", g, field_map=fm)
    t_cached = time.perf_counter() - t0

    ratio = t_uncached / t_cached if t_cached > 0 else float("inf")
    with capsys.disabled():
        print(
            f"\n  decode_frame_fields over {n} frames: "
            f"uncached={t_uncached * 1000:.1f} ms  "
            f"cached={t_cached * 1000:.1f} ms  "
            f"speedup={ratio:.1f}×"
        )

    assert t_cached <= t_uncached, "cached path must not be slower than uncached"
