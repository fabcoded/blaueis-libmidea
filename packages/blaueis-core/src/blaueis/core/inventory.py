"""Field-inventory core — cap-agnostic "what's actually populated on this AC?"

Pure logic that powers two consumers:
  - ``blaueis.tools.field_inventory`` (standalone CLI against a gateway)
  - ``blaueis_midea.field_inventory`` (the HA integration's service + button)

The module never opens a network connection. Consumers own the transport
and push decoded-frame notifications at the ``ShadowDecoder`` via
``observe()``.

Design notes:

- **Cap-gating bypass.** Decoding uses ``decode_frame_fields(
  cap_records=None)`` so ``feature_available`` gates are ignored at the
  codec layer. Fields whose decode steps carry ``encoding: capability``
  fall back to ``float(raw_first_byte)`` — worthless without context.
  For those fields, :func:`decode_variants` re-runs the decoder once per
  ``capability.values.*.raw`` to produce one decoded value per encoding
  variant, letting callers (or humans reading the markdown report)
  pick the plausible one.

- **Classifier.** Five buckets per field: ``populated`` (real data),
  ``zero`` (numerically zero / False / empty string — might be idle,
  might be dead), ``ff_flood`` (whole response body was 0xFF, firmware
  accepted the query but didn't populate it), ``none`` (decoder
  returned ``None``), ``not_seen`` (no response frame carrying this
  field arrived during the scan). See :func:`classify`.

- **Suggested overrides.** :func:`synthesize_override_snippet` builds a
  copy-paste-ready glossary-override YAML fragment for every field
  that (a) was classified ``populated`` and (b) would be hidden in HA
  under the current cap state. Every snippet is validated against
  ``glossary_schema.json`` before being emitted — invalid snippets
  are dropped + logged, never returned to the caller.

- **Transport-agnostic.** ``ShadowDecoder`` exposes ``observe(protocol_key,
  body_bytes)``. The CLI and HA integration each hook whatever ingress
  path they have (raw WS read-loop / ``Device._process_frame`` observer
  slot) and call ``observe()``. The decoder owns no I/O, no asyncio,
  no gateway types.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blaueis.core.codec import decode_frame_fields, walk_fields
from blaueis.core.glossary_override import apply_override

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
#   Field-by-frame reverse index
# ══════════════════════════════════════════════════════════════════════════


def build_frame_field_index(glossary: dict) -> dict[str, list[str]]:
    """Return ``{protocol_key: [field_names]}`` for every field that declares
    a ``protocols.<protocol_key>`` entry.

    Used to predict which fields should populate in response to a given
    frame and to mark the remaining fields as ``not_seen`` when no
    matching frame arrives.
    """
    index: dict[str, list[str]] = {}
    for field_name, field_def in walk_fields(glossary).items():
        for protocol_key in field_def.get("protocols", {}):
            index.setdefault(protocol_key, []).append(field_name)
    return index


def cap_dependent_fields(glossary: dict) -> set[str]:
    """Return the set of field names whose decode uses ``encoding:
    capability`` in any protocol entry.

    These are the fields that produce nonsense under cap-agnostic
    decode and need :func:`decode_variants` to be useful.
    """
    result: set[str] = set()
    for field_name, field_def in walk_fields(glossary).items():
        for protocol_entry in field_def.get("protocols", {}).values():
            for step in protocol_entry.get("decode", []) or []:
                if step.get("encoding") == "capability":
                    result.add(field_name)
                    break
    return result


# ══════════════════════════════════════════════════════════════════════════
#   Classifier
# ══════════════════════════════════════════════════════════════════════════


# Classification buckets — stable strings for JSON / markdown consumers.
CLASS_POPULATED = "populated"
CLASS_ZERO = "zero"
CLASS_FF_FLOOD = "ff_flood"
CLASS_NONE = "none"
CLASS_NOT_SEEN = "not_seen"

ALL_CLASSIFICATIONS = (
    CLASS_POPULATED,
    CLASS_ZERO,
    CLASS_FF_FLOOD,
    CLASS_NONE,
    CLASS_NOT_SEEN,
)


def classify(value: Any, raw_frame_body: bytes | None = None) -> str:
    """Bucket a decoded field value into one of the five classifications.

    - ``None`` → ``none``.
    - ``0`` / ``0.0`` / ``False`` / ``""`` → ``zero``.
    - If ``raw_frame_body`` is provided and consists entirely of ``0xFF``
      bytes → ``ff_flood`` (firmware accepted the query without
      populating anything).
    - Otherwise → ``populated``.

    ``not_seen`` is produced by the orchestrator when no response frame
    carrying this field arrived — not by this function.
    """
    if value is None:
        return CLASS_NONE
    if isinstance(value, bool):
        return CLASS_ZERO if value is False else CLASS_POPULATED
    if isinstance(value, (int, float)) and value == 0:
        return CLASS_ZERO
    if isinstance(value, str) and not value:
        return CLASS_ZERO
    if raw_frame_body and all(b == 0xFF for b in raw_frame_body):
        return CLASS_FF_FLOOD
    return CLASS_POPULATED


# ══════════════════════════════════════════════════════════════════════════
#   Multi-variant decode for cap-dependent fields
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Variant:
    """One cap-value's interpretation of a cap-dependent field."""

    cap_value_name: str  # e.g. "power_cal_non_bcd"
    raw: int  # the cap raw value this variant corresponds to (e.g. 4)
    encoding: str | None  # e.g. "power_linear_4" (None if the value def skips encoding)
    value: Any  # the decoded value under this variant, or None
    feature_available: str | None  # the cap value's feature_available, if declared


def decode_variants(
    field_name: str,
    field_def: dict,
    protocol_key: str,
    body: bytes,
    glossary: dict,
) -> list[Variant]:
    """Re-decode a cap-dependent field once per ``capability.values.*.raw``
    by synthesising a cap record for each variant.

    Only produces output for fields whose decode step is
    ``encoding: capability``. Other fields decode the same under every
    variant — callers should use the single-shot decode instead.
    """
    cap_def = field_def.get("capability") or {}
    cap_id = cap_def.get("cap_id")
    values = cap_def.get("values") or {}
    if not (cap_id and values):
        return []

    cap_id_lower = cap_id.lower()
    cap_type = 2 if cap_def.get("cap_type") == "extended" else 0

    variants: list[Variant] = []
    for value_name, vdef in values.items():
        raw_val = vdef.get("raw")
        if raw_val is None:
            continue
        fake_caps = [
            {
                "cap_id": cap_id,
                "cap_type": cap_type,
                "key_16": f"0x{cap_type:02X}{int(cap_id_lower, 16):02X}",
                "data_len": 1,
                "data": [raw_val],
                "data_hex": f"{raw_val:02x}",
            }
        ]
        decoded = decode_frame_fields(body, protocol_key, glossary, cap_records=fake_caps)
        entry = decoded.get(field_name) or {}
        variants.append(
            Variant(
                cap_value_name=value_name,
                raw=raw_val,
                encoding=vdef.get("encoding"),
                value=entry.get("value"),
                feature_available=vdef.get("feature_available"),
            )
        )
    return variants


def pick_variant(variants: list[Variant], field_def: dict) -> tuple[Variant | None, bool]:
    """Choose the best variant to use in a suggested-override snippet.

    Returns ``(picked_variant, is_guessed)`` where ``is_guessed`` is
    True when the pick wasn't constrained by glossary ``range:`` bounds
    and the user should eyeball the value.

    Heuristic:

    1. If only one variant produces a non-None, non-zero value → pick it, not guessed.
    2. Otherwise, filter to variants whose ``feature_available`` is not
       ``never``, that have an ``encoding`` set, and whose decoded value
       falls within the field's glossary ``range: [min, max]`` bounds
       when present.
    3. If that leaves exactly one candidate → pick it, not guessed.
    4. Otherwise pick the first non-None-valued candidate, flagged as guessed.
    5. If no variant produced a usable value → ``(None, False)``.
    """
    if not variants:
        return None, False

    meaningful = [v for v in variants if v.value is not None and v.value != 0]
    if not meaningful:
        # Every variant is None or zero — nothing worth unlocking. The
        # caller will skip emitting a snippet for this field.
        return None, False

    if len(meaningful) == 1:
        return meaningful[0], False

    # Constrain to "real" variants: has encoding, not marked never.
    candidates = [v for v in meaningful if v.encoding and v.feature_available != "never"]

    # Range-based filter if the glossary declares bounds.
    rng = field_def.get("range")
    if rng and len(rng) == 2:
        lo, hi = rng
        candidates = [v for v in candidates if lo <= v.value <= hi]

    if len(candidates) == 1:
        return candidates[0], False
    if candidates:
        # Multiple plausible variants and no hard discriminator. First wins, flagged.
        return candidates[0], True
    # No "clean" candidate (no encoding / never-flagged / out of range) —
    # fall back to the first meaningful variant, flagged as guessed.
    return meaningful[0], True


# ══════════════════════════════════════════════════════════════════════════
#   Suggested-override synthesizer
# ══════════════════════════════════════════════════════════════════════════


# HA-metadata inference table. Keyed on canonical unit strings (as
# written in glossary `unit:` entries). Extend as new units appear;
# if this table grows beyond ~15 entries move it into the glossary
# as a top-level `unit_presets:` block.
_HA_UNIT_PRESETS: dict[str, dict[str, Any]] = {
    "kWh": {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": "kWh",
        "suggested_display_precision": 2,
        "off_behavior": "available",
    },
    "Wh": {
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": "Wh",
        "suggested_display_precision": 0,
        "off_behavior": "available",
    },
    "kW": {
        "device_class": "power",
        "state_class": "measurement",
        "unit_of_measurement": "kW",
        "suggested_display_precision": 3,
    },
    "W": {
        "device_class": "power",
        "state_class": "measurement",
        "unit_of_measurement": "W",
        "suggested_display_precision": 0,
    },
    "°C": {
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "suggested_display_precision": 1,
        "off_behavior": "available",
    },
    "°F": {
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°F",
        "suggested_display_precision": 1,
        "off_behavior": "available",
    },
    "Hz": {
        "device_class": "frequency",
        "state_class": "measurement",
        "unit_of_measurement": "Hz",
        "suggested_display_precision": 0,
    },
    "V": {
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "suggested_display_precision": 1,
    },
    "A": {
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "A",
        "suggested_display_precision": 2,
    },
    "min": {
        "device_class": "duration",
        "state_class": "measurement",
        "unit_of_measurement": "min",
    },
    "s": {
        "device_class": "duration",
        "state_class": "measurement",
        "unit_of_measurement": "s",
    },
    "h": {
        "device_class": "duration",
        "state_class": "measurement",
        "unit_of_measurement": "h",
    },
    "days": {
        "device_class": "duration",
        "state_class": "measurement",
        "unit_of_measurement": "d",
    },
}


def _infer_ha_metadata(field_name: str, field_def: dict) -> dict[str, Any]:
    """Return an ``ha:`` block dict derived from the field's glossary
    ``unit:`` + ``data_type:`` via ``_HA_UNIT_PRESETS``.

    Special case: ``%`` units map to ``humidity`` when the field name
    contains ``humid``, else ``power_factor``. This is the only
    name-dependent rule — everything else is unit-driven.

    Returns ``{}`` for units we don't have a preset for; the synthesizer
    then emits a snippet with no ``ha:`` block (the cap unlock still
    stands, the entity just lacks display metadata).
    """
    unit = field_def.get("unit")
    if unit is None:
        return {}

    if unit == "%":
        dc = "humidity" if "humid" in field_name else "power_factor"
        return {
            "device_class": dc,
            "state_class": "measurement",
            "unit_of_measurement": "%",
        }

    preset = _HA_UNIT_PRESETS.get(unit)
    return dict(preset) if preset else {}


def _find_category(field_name: str, glossary: dict) -> str | None:
    """Return the top-level ``fields.<category>`` name that contains
    ``field_name``, or ``None`` if not found. Used to build the full
    wrapping path for override snippets.

    Nested sub-categories return the top-level ancestor, matching the
    glossary-override format's nesting semantics.
    """
    fields_root = glossary.get("fields", {})
    for category, subtree in fields_root.items():
        if _contains_field(subtree, field_name):
            return category
    return None


def _contains_field(subtree: dict, field_name: str) -> bool:
    """Recursive search for ``field_name`` within a category subtree."""
    if not isinstance(subtree, dict):
        return False
    if field_name in subtree:
        # Confirm it's a field definition (has ``data_type``) and not a
        # nested category that happens to be named after a field.
        node = subtree[field_name]
        if isinstance(node, dict) and "data_type" in node:
            return True
    return any(isinstance(value, dict) and _contains_field(value, field_name) for value in subtree.values())


def _matched_cap_value_name(cap_def: dict, cap_records: list[dict] | None) -> str | None:
    """Return the name of the ``capability.values`` entry whose ``raw``
    matches the actual cap byte reported by the device, or ``None`` if
    the cap isn't present in the B5 response.

    Used by the override synthesizer to patch the right cap-value key
    (e.g. ``none_0`` on Q11 where cap 0x16=0 is advertised).
    """
    if not cap_records:
        return None
    cap_id = cap_def.get("cap_id", "").lower()
    for rec in cap_records:
        if rec.get("cap_id", "").lower() != cap_id:
            continue
        data = rec.get("data") or []
        if not data:
            return None
        raw_byte = data[0]
        for value_name, vdef in (cap_def.get("values") or {}).items():
            if vdef.get("raw") == raw_byte:
                return value_name
        return None
    return None


@dataclass
class OverrideSnippet:
    """One suggested glossary-override entry emitted to users."""

    field_name: str
    category: str
    reason: str  # human-readable "why this is hidden today"
    picked_variant: Variant | None
    is_guessed: bool  # True when the encoding choice wasn't firmly discriminated
    picked_value: Any  # effective value being claimed: variant value for cap-fields, direct value otherwise
    ha_metadata: dict[str, Any]
    override_dict: dict  # the override as a nested dict, schema-validatable
    yaml_text: str  # the same override rendered as YAML, ready to paste


def synthesize_override_snippet(
    field_name: str,
    field_def: dict,
    protocol_key: str,
    body: bytes,
    glossary: dict,
    cap_records: list[dict] | None,
    current_value: Any = None,
) -> OverrideSnippet | None:
    """Build a copy-paste-ready glossary-override snippet for a single
    populated-but-hidden field, or ``None`` if no snippet is warranted.

    ``current_value`` is the caller's decoded value for the field in
    the current scan (from :class:`FieldState.value`). For non-cap
    fields, this is the effective value the override unlocks; if it
    is ``None`` or zero the snippet is skipped (nothing meaningful
    to unlock). For cap-dependent fields, the value is taken from
    the picked variant instead.

    Returns ``None`` when:
      - field's ``feature_available`` is already ``always`` / ``readable``
        (nothing to unlock);
      - field is cap-dependent and the multi-variant decoder produced
        no usable variant;
      - field is *not* cap-dependent and ``current_value`` is ``None``
        or zero (no real data to unlock);
      - the generated YAML fails schema validation (caller logs + drops).

    Callers should pair this with :func:`classify`-based filtering so
    only ``populated`` fields are considered.
    """
    import yaml
    from jsonschema import Draft202012Validator

    schema = _load_glossary_schema()
    fa = field_def.get("feature_available")
    if fa in ("always", "readable"):
        return None

    cap_def = field_def.get("capability") or {}
    is_cap_dependent = any(
        step.get("encoding") == "capability"
        for protocol_entry in field_def.get("protocols", {}).values()
        for step in (protocol_entry.get("decode") or [])
    )

    variants: list[Variant] = []
    picked: Variant | None = None
    is_guessed = False
    effective_value: Any = current_value
    if is_cap_dependent:
        variants = decode_variants(field_name, field_def, protocol_key, body, glossary)
        picked, is_guessed = pick_variant(variants, field_def)
        if picked is None or picked.encoding is None:
            return None
        effective_value = picked.value
    else:
        # Non-cap-dependent field: skip if there's no meaningful current
        # value. Stops us from emitting snippets that would unlock a
        # field the device never populates anyway.
        if effective_value in (None, 0, 0.0, False, ""):
            return None

    matched_name = _matched_cap_value_name(cap_def, cap_records)
    category = _find_category(field_name, glossary)
    if category is None:
        log.debug("synthesize: no category for field %s — skipped", field_name)
        return None

    ha_meta = _infer_ha_metadata(field_name, field_def)

    # Build the override dict.
    if matched_name and picked:
        override = {
            "fields": {
                category: {
                    field_name: {
                        "capability": {
                            "values": {
                                matched_name: {
                                    "feature_available": "always",
                                    "encoding": picked.encoding,
                                }
                            }
                        },
                        **({"ha": ha_meta} if ha_meta else {}),
                    }
                }
            }
        }
        reason = (
            f"populated; cap {cap_def.get('cap_id', '?')}={_raw_hex(cap_records, cap_def)} "
            f"currently gates the field to {_matched_cap_fa(cap_def, matched_name) or 'never'}"
        )
    else:
        # Cap absent from B5 (or field isn't cap-dependent but still hidden).
        override = {
            "fields": {
                category: {
                    field_name: {
                        "feature_available": "always",
                        **({"ha": ha_meta} if ha_meta else {}),
                    }
                }
            }
        }
        reason = (
            f"populated; cap {cap_def.get('cap_id', '?')} not present in B5 — "
            f"field-level unlock. A proper quirks entry may be more robust "
            f"(see blaueis-libmidea/.../device_quirks/)."
        )

    # Schema-validate by merging into the base glossary and checking the
    # merged view. Drop errors pre-existing in the base (same pattern as
    # the HA config_flow preflight).
    try:
        merged, _affected, _warnings = apply_override(glossary, override)
        validator = Draft202012Validator(schema)
        base_sigs = {(tuple(e.absolute_path), e.message) for e in validator.iter_errors(glossary)}
        new_errors = [e for e in validator.iter_errors(merged) if (tuple(e.absolute_path), e.message) not in base_sigs]
        if new_errors:
            log.warning(
                "synthesize: dropped override for %s — schema rejected: %s",
                field_name,
                new_errors[0].message[:200],
            )
            return None
    except Exception as e:
        log.warning("synthesize: dropped override for %s — validation failed: %s", field_name, e)
        return None

    yaml_text = yaml.safe_dump(override, default_flow_style=False, sort_keys=False)

    return OverrideSnippet(
        field_name=field_name,
        category=category,
        reason=reason,
        picked_variant=picked,
        is_guessed=is_guessed,
        picked_value=effective_value,
        ha_metadata=ha_meta,
        override_dict=override,
        yaml_text=yaml_text,
    )


def _raw_hex(cap_records: list[dict] | None, cap_def: dict) -> str:
    """Find the cap byte actually reported by the device for this cap."""
    if not cap_records:
        return "absent"
    cap_id_lower = cap_def.get("cap_id", "").lower()
    for rec in cap_records:
        if rec.get("cap_id", "").lower() == cap_id_lower:
            data = rec.get("data") or []
            if data:
                return f"0x{data[0]:02x}"
    return "absent"


def _matched_cap_fa(cap_def: dict, matched_name: str) -> str | None:
    values = cap_def.get("values") or {}
    entry = values.get(matched_name) or {}
    return entry.get("feature_available")


_SCHEMA_CACHE: dict | None = None


def _load_glossary_schema() -> dict:
    """Load ``glossary_schema.json`` next to the glossary YAML. Cached."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    schema_path = Path(__file__).resolve().parent / "data" / "glossary_schema.json"
    with open(schema_path, encoding="utf-8") as f:
        _SCHEMA_CACHE = json.load(f)
    return _SCHEMA_CACHE


# ══════════════════════════════════════════════════════════════════════════
#   ShadowDecoder — the observer that accumulates shadow state
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class FrameObservation:
    """One received frame plus its cap-agnostic decode result."""

    timestamp: str
    protocol_key: str
    body: bytes
    decoded: dict[str, dict]
    # ff_flood marker — true when body is entirely 0xFF. Every field
    # decoded from this frame inherits the flag.
    ff_flood: bool = False


@dataclass
class FieldState:
    """Per-field state accumulated across all observations."""

    value: Any = None
    frame: str | None = None
    body: bytes | None = None
    variants: list[Variant] = field(default_factory=list)
    classification: str = CLASS_NOT_SEEN
    ff_flood_seen: bool = False


class ShadowDecoder:
    """Transparent cap-agnostic decoder that accumulates per-field state
    as frames are observed.

    Usage::

        sd = ShadowDecoder(glossary)
        sd.observe("rsp_0xc1_group4", body_bytes)
        sd.observe("rsp_0xa1", other_body_bytes)
        # ... after collection window ...
        inventory = sd.snapshot(cap_records=<last-known B5>)

    Snapshot produces a :class:`InventoryResult` with every field
    classified. Multiple observes of the same protocol_key overwrite
    each other (last wins) — that matches AC reality: a later frame
    is a fresher read.
    """

    def __init__(self, glossary: dict) -> None:
        self._glossary = glossary
        self._index = build_frame_field_index(glossary)
        self._cap_dependent = cap_dependent_fields(glossary)
        self._walk = walk_fields(glossary)
        self._observations: list[FrameObservation] = []

    def observe(self, protocol_key: str, body: bytes) -> None:
        """Record a received frame. Synchronous; safe to call from any
        context including an HA event-loop ingress path.
        """
        ts = datetime.now(UTC).isoformat()
        try:
            decoded = decode_frame_fields(body, protocol_key, self._glossary, cap_records=None)
        except Exception as e:
            log.debug("ShadowDecoder.observe: decode failed for %s: %s", protocol_key, e)
            return
        self._observations.append(
            FrameObservation(
                timestamp=ts,
                protocol_key=protocol_key,
                body=bytes(body),
                decoded=decoded,
                ff_flood=bool(body) and all(b == 0xFF for b in body),
            )
        )

    def observations(self) -> list[FrameObservation]:
        return list(self._observations)

    def snapshot(self, cap_records: list[dict] | None = None) -> InventoryResult:
        """Collapse all observations into a per-field inventory.

        ``cap_records`` is the device's last-known B5 response, used by
        the override synthesizer to pick the right cap-value key. Pass
        ``None`` if no B5 was captured — snippets will fall back to
        field-level overrides.
        """
        # Latest observation per protocol_key.
        latest: dict[str, FrameObservation] = {}
        for obs in self._observations:
            latest[obs.protocol_key] = obs

        # Initialize state for every field that any known frame carries.
        states: dict[str, FieldState] = {}
        for _protocol_key, fields in self._index.items():
            for fname in fields:
                states.setdefault(fname, FieldState())

        # Populate from latest observations.
        for obs in latest.values():
            for fname, entry in obs.decoded.items():
                state = states.setdefault(fname, FieldState())
                state.value = entry.get("value")
                state.frame = obs.protocol_key
                state.body = obs.body
                state.ff_flood_seen = state.ff_flood_seen or obs.ff_flood
                if fname in self._cap_dependent:
                    field_def = self._walk.get(fname) or {}
                    state.variants = decode_variants(fname, field_def, obs.protocol_key, obs.body, self._glossary)

        # Classify.
        for fname, state in states.items():
            if state.frame is None:
                state.classification = CLASS_NOT_SEEN
                continue
            if state.ff_flood_seen:
                state.classification = CLASS_FF_FLOOD
                continue
            # For cap-dependent fields, use the best variant's value
            # instead of the cap-agnostic fallback (which is usually 0).
            effective_value: Any = state.value
            if fname in self._cap_dependent and state.variants:
                picked, _guessed = pick_variant(state.variants, self._walk.get(fname, {}))
                if picked is not None:
                    effective_value = picked.value
            state.classification = classify(effective_value, state.body)

        return InventoryResult(
            states=states,
            observations=list(self._observations),
            cap_records=list(cap_records or []),
            index=self._index,
        )


@dataclass
class InventoryResult:
    """Output of :meth:`ShadowDecoder.snapshot`. The glue between the
    decoder and the report writers.
    """

    states: dict[str, FieldState]
    observations: list[FrameObservation]
    cap_records: list[dict]
    index: dict[str, list[str]]


# ══════════════════════════════════════════════════════════════════════════
#   Report writers — markdown + JSON
# ══════════════════════════════════════════════════════════════════════════


def generate_json_sidecar(
    result: InventoryResult,
    glossary: dict,
    label: str,
    host: str | None = None,
    suggested_overrides: list[OverrideSnippet] | None = None,
) -> dict:
    """Produce the machine-readable JSON sidecar.

    Shape matches the plan's spec in ``compressed-wibbling-dewdrop.md``:
    top-level ``meta`` / ``queries`` / ``fields`` / ``suggested_overrides``.
    ``queries`` is a list of ``{name, hex, response_hex}`` — the tool's
    raw request/response log.
    """
    meta_version = glossary.get("meta", {}).get("version", "unknown")
    out: dict[str, Any] = {
        "meta": {
            "timestamp": datetime.now(UTC).isoformat(),
            "label": label,
            "glossary_version": meta_version,
        },
        "observations": [
            {
                "timestamp": obs.timestamp,
                "protocol_key": obs.protocol_key,
                "body_hex": obs.body.hex(),
                "ff_flood": obs.ff_flood,
            }
            for obs in result.observations
        ],
        "cap_records": result.cap_records,
        "fields": {},
    }
    if host is not None:
        out["meta"]["host"] = host

    walk = walk_fields(glossary)
    for fname, state in result.states.items():
        fdef = walk.get(fname) or {}
        entry: dict[str, Any] = {
            "classification": state.classification,
            "value": _serialise_value(state.value),
            "frame": state.frame,
            "unit": fdef.get("unit"),
            "cap_state": _describe_cap_state(fdef, result.cap_records),
        }
        if state.variants:
            entry["variants"] = [
                {
                    "cap_value_name": v.cap_value_name,
                    "raw": v.raw,
                    "encoding": v.encoding,
                    "value": _serialise_value(v.value),
                    "feature_available": v.feature_available,
                }
                for v in state.variants
            ]
        out["fields"][fname] = entry

    if suggested_overrides:
        out["suggested_overrides"] = [
            {
                "field": s.field_name,
                "category": s.category,
                "reason": s.reason,
                "picked_encoding": s.picked_variant.encoding if s.picked_variant else None,
                "picked_value": _serialise_value(s.picked_value),
                "is_guessed": s.is_guessed,
                "ha_metadata": s.ha_metadata,
                "yaml_snippet": s.yaml_text,
            }
            for s in suggested_overrides
        ]
    return out


def generate_markdown_report(
    result: InventoryResult,
    glossary: dict,
    label: str,
    host: str | None = None,
    suggested_overrides: list[OverrideSnippet] | None = None,
) -> str:
    """Produce the human-readable markdown report."""
    walk = walk_fields(glossary)
    counts = {c: 0 for c in ALL_CLASSIFICATIONS}
    for state in result.states.values():
        counts[state.classification] += 1

    meta_version = glossary.get("meta", {}).get("version", "unknown")
    now = datetime.now(UTC).isoformat()
    n_queries = len({obs.protocol_key for obs in result.observations})
    n_responses = len(result.observations)
    n_ff = sum(1 for obs in result.observations if obs.ff_flood)

    lines: list[str] = []
    lines.append(f"# Field inventory — {label}")
    lines.append("")
    if host:
        lines.append(f"**Device:** {host}")
    lines.append(f"**Timestamp:** {now}")
    lines.append(f"**Glossary version:** {meta_version}")
    lines.append(
        f"**Queries observed:** {n_queries}    **Responses recorded:** {n_responses}    **FF-flooded frames:** {n_ff}"
    )
    lines.append("")
    lines.append("## Summary by classification")
    lines.append("")
    lines.append("| Classification | Count |")
    lines.append("|---|---|")
    for c in ALL_CLASSIFICATIONS:
        lines.append(f"| {c} | {counts[c]} |")
    lines.append("")

    # Populated fields table.
    populated = [(fname, st) for fname, st in result.states.items() if st.classification == CLASS_POPULATED]
    populated.sort(key=lambda x: (x[1].frame or "", x[0]))
    lines.append(f"## Populated fields ({len(populated)})")
    lines.append("")
    lines.append("| Field | Frame | Value | Unit | Cap |")
    lines.append("|---|---|---|---|---|")
    for fname, st in populated:
        fdef = walk.get(fname) or {}
        unit = fdef.get("unit") or ""
        value_display = _value_for_markdown(fname, st, walk)
        cap_display = _describe_cap_state(fdef, result.cap_records) or ""
        lines.append(f"| `{fname}` | `{st.frame}` | {value_display} | {unit} | {cap_display} |")
    lines.append("")

    # Suggested overrides.
    if suggested_overrides:
        lines.append(f"## Suggested overrides ({len(suggested_overrides)} fields)")
        lines.append("")
        lines.append(
            "These are `populated` on your device but currently hidden in HA by "
            "cap-gating. Paste any snippet below into *Configure → Advanced — "
            "Glossary overrides (YAML)*. Snippets are self-contained — stack "
            "multiple under the same `fields:` root and the YAML merge handles it."
        )
        lines.append("")
        for snip in suggested_overrides:
            lines.append(f"### `{snip.field_name}`")
            lines.append("")
            if snip.picked_variant:
                lines.append(
                    f"- **Live-decoded value:** `{_serialise_value(snip.picked_variant.value)}` "
                    f"via `{snip.picked_variant.encoding}` encoding"
                    + (" (guessed — verify against a physical meter)" if snip.is_guessed else "")
                )
            elif snip.picked_value is not None:
                lines.append(f"- **Live-decoded value:** `{_serialise_value(snip.picked_value)}`")
            lines.append(f"- **Reason:** {snip.reason}")
            lines.append("")
            lines.append("```yaml")
            lines.append(snip.yaml_text.rstrip())
            lines.append("```")
            lines.append("")

    # Zero-valued — may be idle, may be dead.
    zeros = [(fname, st) for fname, st in result.states.items() if st.classification == CLASS_ZERO]
    if zeros:
        zeros.sort(key=lambda x: (x[1].frame or "", x[0]))
        lines.append(f"## Zero-valued fields — may be idle, may be dead ({len(zeros)})")
        lines.append("")
        lines.append("| Field | Frame | Last-seen value |")
        lines.append("|---|---|---|")
        for fname, st in zeros:
            lines.append(f"| `{fname}` | `{st.frame}` | `{_serialise_value(st.value)}` |")
        lines.append("")

    # FF-flooded.
    ff = [(fname, st) for fname, st in result.states.items() if st.classification == CLASS_FF_FLOOD]
    if ff:
        lines.append(f"## FF-flooded fields — firmware accepts the query, doesn't populate ({len(ff)})")
        lines.append("")
        lines.append("| Field | Frame |")
        lines.append("|---|---|")
        for fname, st in sorted(ff, key=lambda x: (x[1].frame or "", x[0])):
            lines.append(f"| `{fname}` | `{st.frame}` |")
        lines.append("")

    # None-decoded.
    nones = [(fname, st) for fname, st in result.states.items() if st.classification == CLASS_NONE]
    if nones:
        lines.append(f"## Decoder returned None ({len(nones)})")
        lines.append("")
        lines.append(
            "Could not decode a value from the observed frame — possibly a "
            "cap-dependent field with no plausible encoding variant."
        )
        lines.append("")

    # Not-seen.
    not_seen_count = counts[CLASS_NOT_SEEN]
    lines.append(f"## Not-seen fields ({not_seen_count}) — no matching response frame this run")
    lines.append("")
    lines.append("Fields omitted here — see the JSON sidecar for the full list.")
    lines.append("")

    return "\n".join(lines)


def generate_compare_report(prev: dict, curr: dict) -> str:
    """Produce a markdown diff report between two JSON sidecars.

    Buckets:
      - **Became populated**: zero/none/not_seen → populated
      - **Stopped being populated**: populated → zero/none/not_seen
      - **Value changed**: both populated, value differs
      - **Stable**: count-only rollup
    """
    prev_fields = prev.get("fields", {})
    curr_fields = curr.get("fields", {})
    all_fields = set(prev_fields) | set(curr_fields)

    became: list[tuple[str, Any, Any]] = []
    stopped: list[tuple[str, Any, Any]] = []
    changed: list[tuple[str, Any, Any]] = []
    stable = 0
    for fname in sorted(all_fields):
        p = prev_fields.get(fname, {"classification": CLASS_NOT_SEEN, "value": None})
        c = curr_fields.get(fname, {"classification": CLASS_NOT_SEEN, "value": None})
        p_pop = p.get("classification") == CLASS_POPULATED
        c_pop = c.get("classification") == CLASS_POPULATED
        if not p_pop and c_pop:
            became.append((fname, p.get("value"), c.get("value")))
        elif p_pop and not c_pop:
            stopped.append((fname, p.get("value"), c.get("value")))
        elif p_pop and c_pop:
            if p.get("value") != c.get("value"):
                changed.append((fname, p.get("value"), c.get("value")))
            else:
                stable += 1

    prev_label = prev.get("meta", {}).get("label", "(unlabelled)")
    curr_label = curr.get("meta", {}).get("label", "(unlabelled)")

    lines = [
        f"# Field inventory comparison — `{prev_label}` → `{curr_label}`",
        "",
        f"- **From:** `{prev_label}` at {prev.get('meta', {}).get('timestamp', '?')}",
        f"- **To:** `{curr_label}` at {curr.get('meta', {}).get('timestamp', '?')}",
        "",
        f"## Became populated ({len(became)})",
        "",
    ]
    if became:
        lines += ["| Field | Was | Now |", "|---|---|---|"]
        for fname, was, now in became:
            lines.append(f"| `{fname}` | `{was}` | `{now}` |")
    else:
        lines.append("_(none)_")
    lines += [
        "",
        f"## Stopped being populated ({len(stopped)})",
        "",
    ]
    if stopped:
        lines += ["| Field | Was | Now |", "|---|---|---|"]
        for fname, was, now in stopped:
            lines.append(f"| `{fname}` | `{was}` | `{now}` |")
    else:
        lines.append("_(none)_")
    lines += [
        "",
        f"## Value changed ({len(changed)})",
        "",
    ]
    if changed:
        lines += ["| Field | Was | Now |", "|---|---|---|"]
        for fname, was, now in changed:
            lines.append(f"| `{fname}` | `{was}` | `{now}` |")
    else:
        lines.append("_(none)_")
    lines += [
        "",
        f"## Stable ({stable})",
        "",
        "Fields populated in both runs with identical values — not listed.",
    ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
#   Internals
# ══════════════════════════════════════════════════════════════════════════


def _describe_cap_state(field_def: dict, cap_records: list[dict]) -> str:
    """Human-readable description of how the cap resolves for this field."""
    fa = field_def.get("feature_available")
    if fa in ("always", "readable", "never"):
        return fa
    cap_def = field_def.get("capability") or {}
    cap_id = cap_def.get("cap_id")
    if not cap_id:
        return fa or ""
    raw_hex = _raw_hex(cap_records, cap_def)
    matched = _matched_cap_value_name(cap_def, cap_records)
    if matched:
        values = cap_def.get("values") or {}
        cap_fa = (values.get(matched) or {}).get("feature_available")
        return f"cap {cap_id}={raw_hex} → {cap_fa or 'never'}"
    return f"cap {cap_id}={raw_hex} (unmatched)"


def _value_for_markdown(field_name: str, state: FieldState, walk: dict) -> str:
    """Render a field value for the populated-table, preferring the
    picked variant when one is available."""
    if state.variants:
        fdef = walk.get(field_name) or {}
        picked, guessed = pick_variant(state.variants, fdef)
        if picked is not None and picked.value is not None:
            via = f" via `{picked.encoding}`" if picked.encoding else ""
            mark = " _(guessed)_" if guessed else ""
            return f"`{_serialise_value(picked.value)}`{via}{mark}"
    return f"`{_serialise_value(state.value)}`"


def _serialise_value(value: Any) -> Any:
    """Coerce decoded values into JSON-safe primitives."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


# ══════════════════════════════════════════════════════════════════════════
#   Deep-copy guard (defensive — we never mutate caller data)
# ══════════════════════════════════════════════════════════════════════════


def safe_glossary(glossary: dict) -> dict:
    """Deep-copy a glossary for callers that want to be sure we won't
    mutate theirs. Not used internally; exposed for consumers that want
    an extra safety layer."""
    return copy.deepcopy(glossary)


__all__ = [
    "ShadowDecoder",
    "InventoryResult",
    "FrameObservation",
    "FieldState",
    "OverrideSnippet",
    "Variant",
    "build_frame_field_index",
    "cap_dependent_fields",
    "classify",
    "decode_variants",
    "pick_variant",
    "synthesize_override_snippet",
    "generate_json_sidecar",
    "generate_markdown_report",
    "generate_compare_report",
    "safe_glossary",
    "ALL_CLASSIFICATIONS",
    "CLASS_POPULATED",
    "CLASS_ZERO",
    "CLASS_FF_FLOOD",
    "CLASS_NONE",
    "CLASS_NOT_SEEN",
]
