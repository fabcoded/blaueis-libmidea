# Feature gating — the offer gate

> How the library decides whether a field is **offered** (surfaced as a
> controllable/observable entity) for the current device state. Gating is a
> three-axis predicate — **capability**, **mode**, **interlock** — over a power
> gate and the existing mutual-exclusion cascade.
>
> **Advisory only.** The offer gate drives UI/entity availability. It never gates
> wire behaviour — the command builder keeps its own minimal mode mask, and the
> wire path stays stateless. Gating rules are derived from the device's own B5
> capability reports (own research); none of it is enforced by the AC firmware.

---

## 1. The predicate

A field is **offered** iff every axis agrees:

```
offered(field) = power_ok(field)                               # gate.requires_power
             AND capability_present(field)                     # feature_available (B5)
             AND mode_in(visible_in_modes ∩ cap_derived_modes) # logical ∩ capability mode
             AND interlocks_ok(field)                          # cross-feature live state
             AND NOT mutex_forced_off(field)                   # mutual_exclusion cascade
```

The three gating axes are **distinct** and must not be conflated:

- **Capability** — *can the hardware do this at all?* (sourced from B5 caps)
- **Mode** — *does it make sense in the current operating mode?* (intrinsic logic)
- **Interlock** — *is a conflicting feature currently engaged?* (runtime live state)

> **Status.** Live: the capability-presence axis, `cap_mode`, the interlock axis
> (with its mode guard), the bit-position anchors, decode-retention, and the
> cap-gating convention (§7). Pending: the **mode-fork** axis (§2.2) and its
> `cap_values` evaluator input — schema-declared and implemented on a branch, held
> for a live engagement test before merge. Sections below mark pending pieces.

## 2. Axes

### 2.1 Capability presence — `feature_available`
The B5 capability pass (`process._apply_caps_to_fields`) decodes each cap byte and
sets the field's `feature_available`. A field whose cap reports "not supported"
(or whose cap is absent and which defaults to `capability`) becomes `excluded` and
is never offered. This axis already worked before the gate engine; see
`exclusion_reasons.md` for the `feature_available` state contract.

### 2.2 Capability-derived mode — `gate.cap_mode` / `gate.mode_forks`
Some caps restrict *which operating modes* a feature applies to. Two forms:

- **`cap_mode: {cap_id}`** — the active cap value's `valid_set` is interpreted as
  **operating-mode raws** (e.g. a turbo-type cap → `[2,4]` = cool+heat). This marker
  is required: a `valid_set` is otherwise a *field-value* constraint, never modes
  (the **value-vs-mode trap**). The axis fires only when the live `valid_set` is all
  operating-mode raws (ints 1–5, excluding bool) — so a pre-B5 permissive default
  like `[False, True]` stays inert and the field falls back to its logical mode rule.
- **`mode_forks: [{cap_id, when_raw, modes}]`** *(pending — eco-variant increment)* — a
  cap-value → explicit mode-set fork a `valid_set` cannot express (e.g. an eco-variant
  cap whose value 1 ⇒ cool-only and value 2 ⇒ cool/auto/dry). First matching fork wins;
  no match ⇒ inert. The schema accepts this today; the evaluator branch and the
  `cap_values` plumbing land with the eco increment after live validation.

Effective modes = `visible_in_modes ∩ cap_mode_set ∩ mode_fork_set` (each absent axis
contributes no restriction).

### 2.3 Logical mode — `ux.visible_in_modes`
The intrinsic "does it make sense now" rule — kept as-is, **not** derived from caps
(e.g. an 8 °C-frost preset is heat-only). See `ux_gating.is_field_visible`.

### 2.4 Interlock — `gate.interlocks`
Block a field while a *different* feature's live state is on/off:

```yaml
interlocks:
  - field: auxiliary_heat_level   # dependency field (its retained value is read)
    at: 'C0:9:4..3'               #   AND its wire address (verified — see §5)
    blocks_when: nonzero          # block when the dependency is truthy (default) or zero/off
    modes: [heat, auto]           # OPTIONAL mode guard (see below)
```

- **`modes:` guard** — required when the dependency bit is *mode-multiplexed* (the
  same physical bit carries different meaning per mode). The interlock applies only
  in the listed modes; outside them the bit means something else and the interlock is
  skipped, so a neighbour field's bit can't spuriously block.
- **Fail-open** — if the dependency value is unknown (cap-absent, not yet decoded),
  the interlock is vacuously satisfied. A gate must never block on a value it can't read.

### 2.5 Power and mutex
- **`requires_power`** (default `true`) — gate on device power. Callers may pass
  `power_on=True` to keep this axis inert where power fading is handled elsewhere.
- **`mutex_group`** — declarative pointer to the existing `mutual_exclusion` cascade,
  which enforces forced-off siblings. Not re-evaluated by the gate evaluator.

## 3. The `gate:` block (glossary authoring)

All keys optional; an absent block reduces gating to `power_on ∧ is_field_visible`
(parity with the pre-engine behaviour). Schema: `$defs/gate_block` in
`glossary_schema.json`.

```yaml
gate:
  requires_power: true
  cap_mode: {cap_id: '0x1A'}
  mode_forks: [{cap_id: '0x12', when_raw: 1, modes: [cool]}]
  interlocks:
    - {field: <dep>, at: 'C0:9:4..3', blocks_when: nonzero, modes: [heat, auto]}
  mutex_group: <name>
```

## 4. The evaluator — `core/gate_eval.py`

```
evaluate_offered(field_gdef, *, mode, power_on, active_constraints=None,
                 field_states=None, caps=None) -> GateVerdict
```

A **pure function** returning `GateVerdict(offered: bool, blocked_by: list[str])`.
`blocked_by` names each failing axis (e.g. `["cap_mode:heat∉['cool']"]`,
`["interlock:auxiliary_heat_level"]`) for debuggability. Inputs mirror live status:

| param | source |
|---|---|
| `mode` / `power_on` | decoded operating_mode / power |
| `active_constraints` | `status['fields'][f]['active_constraints']` (cap-derived) |
| `field_states` | `{name: value}` for interlock dependencies (retained values) |
| `caps` | B5 flag bitmap (for `ux.hardware_flag`) |
| `cap_values` *(pending)* | `{cap_id: raw}` of the unit's B5 caps — added by the eco increment for `mode_forks` |

## 5. Bit-position anchors — `core/gate_anchors.py`

Every gate/interlock reference is **dual-keyed**: a field **name** *and* its physical
**wire address** (`PROTO:OFFSET:HI..LO`, e.g. `C0:8:5..5`; B1-property fields use
`B1:PROP_ID:HI..LO`). The name reads the retained value; the address says where that
value lives on the wire — and the two must agree.

`verify_all_anchors(glossary)` resolves each anchored field's address from its
`decode` block and asserts it matches the declared `at:` — for a seed registry **and**
every `gate.interlocks[].at`. A field rename, a reused name, or a decode-offset edit
makes them disagree and **fails the invariant check** (run in CI), rather than silently
re-aiming a gate at the wrong bit. The address form mirrors the glossary's
`decode: {offset, bits: [hi, lo]}` (high bit first).

## 6. Decode-retention vs exposure

Decoding, **retention**, and **exposure** are independent concerns:

1. **Decode** — extract the value from the frame.
2. **Retain** — store it in `status` so anything can read it.
3. **Expose** — surface it as an entity / poll it.

`process_data_frame` retains a decoded value for a confirmed-**excluded** field, so a
gating interlock can read its live state even though the field is hidden. Only the
*pre-B5* `capability` / `capability-opt` window is skipped (the decode is untrusted
until a cap confirms which feature owns the byte). Exposure and polling stay gated at
`available_fields` / `required_queries`. This is why an interlock can read, say, an
elec-heat state that is itself not exposed as an entity.

## 7. Cap-gating convention — `capability` vs `readable`

A field whose **capability can declare it unavailable** (its `capability.values`
includes an `excluded`/`none` entry) must default to `feature_available: capability`
— **hidden until a present cap promotes it**. Do **not** use `readable` for such a
field: `readable` exposes it unless a cap *demotes* it, but when the cap is **absent**
the cap pass never runs, so the field never demotes and surfaces as a permanently-dead
entity on units that lack the feature.

Rule of thumb:
- cap can exclude the feature ⇒ **`capability`** (hide-until-promoted).
- feature is unconditional (no cap, or cap never says "not supported") ⇒ `readable`/`always`.

## 8. Advisory boundary

The offer gate (`evaluate_offered`) governs **availability/offering** only. The
command builder (`command.build_command_body`) keeps its own small
`is_field_visible` mode mask to zero stale bits on outgoing frames; it does **not**
consult the gate engine. Keeping the wire path independent of runtime gate state
preserves the stateless-wire invariant.

---

## Source map
- model + evaluator: `core/gate_eval.py`
- anchors: `core/gate_anchors.py`
- schema: `core/data/glossary_schema.json` (`$defs/gate_block`)
- capability ingestion: `core/process.py` (`_apply_caps_to_fields`)
- `feature_available` contract: `docs/exclusion_reasons.md`
- HA-side wiring: `blaueis-ha-midea/docs/feature_gating.md`
