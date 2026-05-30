# Glossary companion — deferred work & design sketches

> The glossary (`glossary.yaml`) is the protocol data source-of-truth. A
> handful of entries reference this doc from their `sources:` or `note:`
> by stable section number — for codec frames sketched but not yet built
> (§6) and for excluded fields' specific reopen conditions (§13). This is
> the living home for that deferred work, so an exclusion or a hypothesis
> entry points somewhere real instead of carrying a dead promise.
>
> Section numbers are stable anchors: glossary entries cite `§6` / `§13`,
> so those numbers don't get renumbered. Unlisted numbers are simply
> unused — add a new section with the next free number rather than
> reusing one.
>
> **Public-repo doc.** Everything here is written in our own words. No
> source paths, symbol names, line references, or copied material — same
> leak bar as the glossary `note:` fields.

## §6 — Codec design sketches (frames not yet built)

Frames the protocol model documents but our Python codec does not yet
assemble. Each is referenced from the matching `cmd_*` glossary entry's
`sources:`. The sketch records *why it's deferred* and *what building it
would take*, so the glossary entry can stay `confidence: hypothesis`
honestly.

### §6.1 — `cmd_0x41_ext` (extended state query, sub-page 0x02)

- **Status:** not built by our codec; not observed in our captures.
- **Why it matters:** three glossary fields decode from its response
  (`rsp_0xc1_sub02`), so *some* controller issues this query — just not
  the OEM WiFi dongle or extension board we have on the bench.
- **To build:** assemble the fixed selector body (the `bytes_at` map on
  the glossary entry documents the selector positions) and route the
  `rsp_0xc1_sub02` reply through the existing C1 sub-page parser.
- **To verify:** a capture from a controller that does emit it, or a
  bench unit that answers the query we synthesize — confirming the three
  dependent fields populate as decoded.

### §6.2 — `cmd_0xb0` property-set builder

- **Status:** constructed on demand today, no general builder.
- **Why it matters:** property-only fields (ionizer, breezeless, the
  persistent buzzer alternative, and similar) each hand-roll their B0
  frame; a shared builder would remove the duplication and make new
  property writes a data-only change.
- **To build:** a body assembler that walks each field's
  `protocols.cmd_0xb0` entry (property ID + data bytes) the way the
  `cmd_0x40` builder walks `decode` arrays.
- **To verify:** round-trip — write via the builder, read the value back
  via `rsp_0xb1` / `rsp_0xb1_prop`, confirm it matches.

## §13 — Reopen conditions for excluded fields

Per-field, the *specific* observation that would justify moving a field
out of `feature_available: excluded`. Complements the *generic* recovery
column in `exclusion_reasons.md` — that doc says what kind of evidence a
reason needs; this says what it concretely looks like for the field.

### §13.1 — `rate_select` (`unknown_semantic`)

- **Current disposition:** excluded. The byte is passive — it resets to
  the sentinel `100` ("off/disabled") on every power and mode
  transition, so a non-`100` value is not user-sustainable. The B5
  capability values (`gear_2` / `gen_mode` / `gear_5`) describe the
  supported speed-mode *type*, not the runtime byte; the runtime
  sub-rate meanings are undecoded.
- **Reopen condition:** a `gen_mode` or `gear_5` device on which the
  sub-rate byte is **durable** (survives a mode change), **mode-stable**
  (doesn't snap back to `100`), and **settable** (a written sub-rate
  reads back and persists). On such a unit the byte carries real state
  and the sub-rate values become decodable from paced set/read-back
  captures.
- **On reopen:** decode the sub-rate value set, drop `unknown_semantic`,
  and re-tier from `excluded` to `capability` (B5-gated on cap `0x48`).
