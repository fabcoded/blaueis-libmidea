"""Atomic state layer for the AC status database.

Wraps the glossary-driven status dict with an asyncio.Lock that
serializes INGEST (AC responses) and COMMAND (set calls). Reads are
lock-free. Callbacks are batched and flushed after lock release.

See docs/status_db.md for the full design.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from blaueis.core.codec import load_glossary, walk_fields
from blaueis.core.command import build_b0_command_body, build_command_body
from blaueis.core.frame import build_frame
from blaueis.core.process import process_data_frame
from blaueis.core.query import read_field, write_field
from blaueis.core.status import build_status
from blaueis.core.ux_gating import default_for_masked_field, is_field_visible

log = logging.getLogger("blaueis.device")

# Sentinel returned by _clamp_to_envelope when a disabled cap
# (valid_set=[]) should cause the write to be dropped entirely.
_DROP = object()


class StatusDB:
    """Atomic wrapper around the glossary-driven status dict.

    Serializes INGEST and COMMAND operations via asyncio.Lock.
    READs are lock-free (consistent within a single event-loop tick).
    Callbacks are batched and flushed after lock release.
    """

    def __init__(self, glossary: dict | None = None):
        if glossary is None:
            glossary = load_glossary()
        self._glossary = glossary
        self._status: dict = build_status(glossary=glossary)
        self._lock = asyncio.Lock()
        self._pending_events: list[tuple[str, object, object]] = []
        self.on_state_change: Callable[[str, object, object], None] | None = None

    @property
    def status(self) -> dict:
        return self._status

    @property
    def glossary(self) -> dict:
        return self._glossary

    # ── READ (lock-free) ──────────────────────────────────

    def read(self, field_name: str) -> object | None:
        r = read_field(self._status, field_name)
        return r["value"] if r else None

    def read_field(self, field_name: str) -> dict | None:
        return read_field(self._status, field_name)

    # ── INGEST (locked) ───────────────────────────────────

    async def ingest(
        self,
        body: bytes,
        protocol_key: str,
        *,
        timestamp: str | None = None,
        available_fields: dict | None = None,
    ) -> None:
        """Decode an AC response frame and update the status dict.

        Acquires the lock, snapshots current values, processes the frame,
        detects changes, and flushes callbacks after release.
        """
        if timestamp is None:
            timestamp = datetime.now(UTC).isoformat()
        async with self._lock:
            snapshot = self._snapshot(available_fields)
            process_data_frame(
                self._status, body, protocol_key, self._glossary,
                timestamp=timestamp,
            )
            self._detect_changes(snapshot, available_fields)
        self._flush_events()

    # ── COMMAND (locked) ──────────────────────────────────

    async def command(
        self,
        changes: dict,
        send_fn: Callable,
    ) -> dict:
        """Mode-gate, expand mutex, build frames, send, optimistic write.

        All steps run under one lock hold. Callbacks flush after release.

        Returns:
            {
                "expanded": dict,   # changes after mode gate + mutex expansion
                "rejected": dict,   # {field: reason} for mode-gated fields
                "results": dict,    # per-frame build results
            }
        """
        async with self._lock:
            all_fields = walk_fields(self._glossary)

            # Step 1: Mode gate — reject fields not valid in current mode
            gated, rejected = self._apply_mode_gate(changes, all_fields)

            # Step 2: Mutex expansion — forward + reverse pass
            expanded = self._expand_mutex_forces(gated, all_fields)

            # Step 2.5: Constraint gate — clamp to active cap envelope.
            # Runs AFTER mutex expansion so glossary-forced values are
            # re-validated: any clamp at this stage signals a glossary
            # inconsistency (mutex force outside active_constraints).
            expanded = self._apply_constraint_gate(
                expanded, all_fields, effective_mode=expanded.get(
                    "operating_mode", self.read("operating_mode"),
                ),
            )

            # Step 3: Split by protocol, build frames, send
            x40, b0 = self._split_by_protocol(expanded, all_fields)
            results = {}

            if x40:
                result = build_command_body(
                    self._status, x40, self._glossary,
                )
                if result["body"] is not None:
                    frame = build_frame(result["body"], msg_type=0x02)
                    await send_fn(frame.hex(" "))
                    log.info("Sent 0x40 command: %s", x40)
                    results["cmd_0x40"] = result
                elif result.get("preflight"):
                    log.warning(
                        "Command blocked by preflight: %s", result["preflight"],
                    )
                    results["cmd_0x40"] = result

            if b0:
                result = build_b0_command_body(
                    self._status, b0, self._glossary,
                )
                if result["body"] is not None:
                    frame = build_frame(result["body"], msg_type=0x02)
                    await send_fn(frame.hex(" "))
                    log.info("Sent 0xB0 command: %s", b0)
                    results["cmd_0xb0"] = result

            # Step 4: Optimistic write — under same lock hold
            self._apply_optimistic(expanded)

        self._flush_events()
        return {"expanded": expanded, "rejected": rejected, "results": results}

    # ── Mode gate ─────────────────────────────────────────

    def _apply_mode_gate(
        self, changes: dict, all_fields: dict,
    ) -> tuple[dict, dict]:
        """Check visible_in_modes for each field. Reject wrong-mode writes.

        Returns (accepted_changes, rejected) where rejected maps
        field name to reason string.
        """
        current_mode = self.read("operating_mode")

        # If operating_mode is being changed in this call, use the NEW
        # mode for gating the other fields — the caller intends to switch.
        effective_mode = changes.get("operating_mode", current_mode)

        accepted = {}
        rejected = {}

        for fname, value in changes.items():
            gdef = all_fields.get(fname, {})
            modes = (gdef.get("ux") or {}).get("visible_in_modes")
            if modes is None:
                accepted[fname] = value
                continue
            if is_field_visible(gdef, current_mode=effective_mode):
                accepted[fname] = value
            else:
                rejected[fname] = f"requires mode {modes}, current={effective_mode}"
                log.warning(
                    "Mode gate rejected %s=%r: visible_in_modes=%s, "
                    "effective_mode=%s",
                    fname, value, modes, effective_mode,
                )

        return accepted, rejected

    # ── Constraint gate ───────────────────────────────────

    def _apply_constraint_gate(
        self, changes: dict, all_fields: dict, effective_mode: object,
    ) -> dict:
        """Clamp values to the active cap envelope.

        Policy: always clamp to the nearest valid value. Silent to the
        caller; log.warning for the audit trail. Clamping is protective —
        it prevents out-of-range writes from reaching the wire when the
        caller (or a mutex force) has drifted from the active cap.

        Envelope resolution:
          - status["fields"][fname]["active_constraints"]
          - If `by_mode` present, pick entry for `effective_mode`
          - Else use the top-level envelope

        Bounds handled:
          - valid_range: clamp to [lo, hi]
          - valid_set: snap to nearest member (disabled caps with
            valid_set=[] drop the write with a warning)

        Skips:
          - data_type == "bool" (no bounds applicable)
          - active_constraints missing/empty (pre-B5 boot)
        """
        out = dict(changes)
        for fname, value in list(out.items()):
            gdef = all_fields.get(fname, {})
            if gdef.get("data_type") == "bool":
                continue

            fstate = self._status.get("fields", {}).get(fname, {})
            ac = fstate.get("active_constraints") or {}
            if not ac:
                continue

            envelope = ac
            by_mode = ac.get("by_mode")
            if isinstance(by_mode, dict) and effective_mode is not None:
                mode_name = self._mode_label(effective_mode)
                if mode_name and mode_name in by_mode:
                    envelope = by_mode[mode_name]

            clamped = self._clamp_to_envelope(value, envelope)
            if clamped is _DROP:
                log.warning(
                    "constraint gate: dropped %s=%r (feature disabled on "
                    "this unit, valid_set empty)",
                    fname, value,
                )
                out.pop(fname, None)
                continue
            if clamped != value:
                log.warning(
                    "constraint gate: clamped %s=%r → %r "
                    "(envelope=%s, mode=%s)",
                    fname, value, clamped,
                    self._envelope_summary(envelope),
                    effective_mode,
                )
                out[fname] = clamped
        return out

    @staticmethod
    def _clamp_to_envelope(value: object, envelope: dict) -> object:
        """Clamp to valid_range or snap to valid_set. Returns _DROP when
        the cap is disabled (valid_set=[])."""
        valid_set = envelope.get("valid_set")
        if valid_set is not None:
            if not valid_set:
                return _DROP
            try:
                return min(valid_set, key=lambda x: abs(x - value))
            except TypeError:
                return value

        valid_range = envelope.get("valid_range")
        if isinstance(valid_range, (list, tuple)) and len(valid_range) == 2:
            lo, hi = valid_range
            try:
                return max(lo, min(hi, value))
            except TypeError:
                return value

        return value

    def _mode_label(self, raw_mode: object) -> str | None:
        """Map operating_mode raw (int) to its glossary label (cool, heat, ...)."""
        gdef = (
            self._glossary.get("fields", {})
            .get("control", {})
            .get("operating_mode", {})
        )
        for label, entry in (gdef.get("values") or {}).items():
            if isinstance(entry, dict) and entry.get("raw") == raw_mode:
                return label
        return None

    @staticmethod
    def _envelope_summary(envelope: dict) -> str:
        if "valid_range" in envelope:
            return f"range={envelope['valid_range']}"
        if "valid_set" in envelope:
            return f"set={envelope['valid_set']}"
        return "none"

    # ── Mutex expansion ───────────────────────────────────

    def _expand_mutex_forces(
        self, changes: dict, all_fields: dict,
    ) -> dict:
        """Expand mutual_exclusion.when_on.forces for active fields.

        Forward pass: fields being set to truthy values have their forces
        merged into the result. Transitive via work queue, depth-capped.

        Reverse pass: if operating_mode changed, clear fields not visible
        in the new mode that are currently ON in the status DB.
        """
        expanded = dict(changes)

        # Forward pass — truthy fields trigger their forces
        queue = [
            f for f, v in expanded.items()
            if self._is_active_value(all_fields.get(f, {}), v)
        ]
        seen = set(queue)
        depth = 0
        max_depth = 10

        while queue and depth < max_depth:
            depth += 1
            next_queue = []
            for fname in queue:
                gdef = all_fields.get(fname, {})
                forces = (
                    gdef.get("mutual_exclusion", {})
                    .get("when_on", {})
                    .get("forces", {})
                )
                for target, forced_val in forces.items():
                    if target in expanded:
                        continue
                    expanded[target] = forced_val
                    if (
                        target not in seen
                        and self._is_active_value(
                            all_fields.get(target, {}), forced_val,
                        )
                    ):
                        next_queue.append(target)
                        seen.add(target)
            queue = next_queue

        if depth >= max_depth:
            log.warning("Mutex expansion hit depth cap (%d)", max_depth)

        # Reverse pass — mode change clears incompatible fields
        if "operating_mode" in expanded:
            new_mode = expanded["operating_mode"]
            for fname, gdef in all_fields.items():
                if fname in expanded:
                    continue
                modes = (gdef.get("ux") or {}).get("visible_in_modes")
                if modes is None:
                    continue
                if not is_field_visible(gdef, current_mode=new_mode):
                    current = read_field(self._status, fname)
                    current_val = current["value"] if current else None
                    if current_val and current_val != 0:
                        expanded[fname] = default_for_masked_field(gdef)

        if len(expanded) > len(changes):
            log.debug(
                "Mutex expanded %d → %d fields: +%s",
                len(changes),
                len(expanded),
                sorted(set(expanded) - set(changes)),
            )

        return expanded

    # ── Internal helpers ──────────────────────────────────

    @staticmethod
    def _is_active_value(gdef: dict, value: object) -> bool:
        dt = gdef.get("data_type", "")
        if dt == "bool":
            return bool(value)
        return value is not None and value != 0

    @staticmethod
    def _split_by_protocol(
        changes: dict, all_fields: dict,
    ) -> tuple[dict, dict]:
        x40 = {}
        b0 = {}
        for fname, value in changes.items():
            gdef = all_fields.get(fname, {})
            protocols = gdef.get("protocols", {})
            if "cmd_0xb0" in protocols:
                b0[fname] = value
            else:
                x40[fname] = value
        return x40, b0

    def _apply_optimistic(self, changes: dict) -> None:
        for fname, new_val in changes.items():
            old = read_field(self._status, fname)
            old_val = old["value"] if old else None
            if old_val == new_val:
                continue
            try:
                write_field(self._status, fname, new_val)
            except Exception:
                log.exception("optimistic write failed for %s", fname)
                continue
            self._pending_events.append((fname, new_val, old_val))

    def _snapshot(self, available_fields: dict | None) -> dict[str, object]:
        if available_fields is None:
            return {}
        snap = {}
        for fname in available_fields:
            r = read_field(self._status, fname)
            snap[fname] = r["value"] if r else None
        return snap

    def _detect_changes(
        self, snapshot: dict[str, object], available_fields: dict | None,
    ) -> None:
        if available_fields is None:
            return
        for fname in available_fields:
            r = read_field(self._status, fname)
            new_val = r["value"] if r else None
            old_val = snapshot.get(fname)
            if new_val != old_val:
                self._pending_events.append((fname, new_val, old_val))

    def _flush_events(self) -> None:
        """Fire batched callbacks OUTSIDE the lock. Deduplicate."""
        if not self.on_state_change or not self._pending_events:
            self._pending_events.clear()
            return

        merged: dict[str, tuple[object, object]] = {}
        for fname, new_val, old_val in self._pending_events:
            if fname in merged:
                _, first_old = merged[fname]
                merged[fname] = (new_val, first_old)
            else:
                merged[fname] = (new_val, old_val)
        self._pending_events.clear()

        for fname, (new_val, old_val) in merged.items():
            if new_val == old_val:
                continue
            try:
                self.on_state_change(fname, new_val, old_val)
            except Exception:
                log.exception("on_state_change callback error for %s", fname)
