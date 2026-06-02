"""Regression guard: cap-gated comfort features must hide until their cap confirms.

silky_cool (0x18) and humidity_setpoint (0x1F) previously defaulted to 'readable',
which left them exposed as dead entities on units that never advertise the cap
(cap-absent ⇒ _apply_caps_to_fields never demotes them). They must default to
'capability' — hidden until B5 promotes — like the rest of the cap-gated family.
See the cap-coverage sweep.
"""
from __future__ import annotations

from blaueis.core.codec import load_glossary, walk_fields


def test_cap_gated_comfort_fields_default_capability() -> None:
    fields = walk_fields(load_glossary())
    for name in ("silky_cool", "humidity_setpoint"):
        fd = fields[name]
        assert fd["feature_available"] == "capability", (
            f"{name} must be cap-gated (capability), not "
            f"{fd['feature_available']!r} — it over-exposes on cap-absent units"
        )
        # sanity: the cap really can declare it unavailable (so hiding is correct)
        vals = (fd.get("capability") or {}).get("values") or {}
        assert any(v.get("feature_available") == "excluded" for v in vals.values()), name
