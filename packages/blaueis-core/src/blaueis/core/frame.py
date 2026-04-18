"""Midea UART frame construction and parsing — full toolkit version.

This is the canonical frame builder for the serial protocol toolkit.
It contains ALL frame builders: queries, set commands, capability queries,
group page queries, etc. Used by the client to build frames before
transmitting them as hex strings over the WebSocket gateway.

The gateway has its own minimal copy (gateway/midea_frame.py) with only
the frames needed for protocol maintenance (handshake, network status
reply, version reply). All application-layer frames live HERE.

Frame layout (UART wire format):
  [0]  0xAA start byte
  [1]  LEN  (total frame length minus 1, i.e. bytes 1..N)
  [2]  TYPE (appliance type, e.g. 0xAC for AC)
  [3]  SYNC (LEN ^ TYPE)
  [4]  0x00 reserved
  [5]  0x00 reserved
  [6]  SEQ  sequence number
  [7]  PROTO protocol version
  [8]  SUB  device sub-type
  [9]  MSG  message type
  [10..N-2] BODY payload
  [N-1] CRC8 over body (bytes 10..N-2)
  [N]   CHECKSUM over bytes 1..N-1
"""

# ── CRC-8/MAXIM (Dallas/DOW) ─────────────────────────────────────────────

# fmt: off
CRC8_TABLE = [
    0x00, 0x5E, 0xBC, 0xE2, 0x61, 0x3F, 0xDD, 0x83,
    0xC2, 0x9C, 0x7E, 0x20, 0xA3, 0xFD, 0x1F, 0x41,
    0x9D, 0xC3, 0x21, 0x7F, 0xFC, 0xA2, 0x40, 0x1E,
    0x5F, 0x01, 0xE3, 0xBD, 0x3E, 0x60, 0x82, 0xDC,
    0x23, 0x7D, 0x9F, 0xC1, 0x42, 0x1C, 0xFE, 0xA0,
    0xE1, 0xBF, 0x5D, 0x03, 0x80, 0xDE, 0x3C, 0x62,
    0xBE, 0xE0, 0x02, 0x5C, 0xDF, 0x81, 0x63, 0x3D,
    0x7C, 0x22, 0xC0, 0x9E, 0x1D, 0x43, 0xA1, 0xFF,
    0x46, 0x18, 0xFA, 0xA4, 0x27, 0x79, 0x9B, 0xC5,
    0x84, 0xDA, 0x38, 0x66, 0xE5, 0xBB, 0x59, 0x07,
    0xDB, 0x85, 0x67, 0x39, 0xBA, 0xE4, 0x06, 0x58,
    0x19, 0x47, 0xA5, 0xFB, 0x78, 0x26, 0xC4, 0x9A,
    0x65, 0x3B, 0xD9, 0x87, 0x04, 0x5A, 0xB8, 0xE6,
    0xA7, 0xF9, 0x1B, 0x45, 0xC6, 0x98, 0x7A, 0x24,
    0xF8, 0xA6, 0x44, 0x1A, 0x99, 0xC7, 0x25, 0x7B,
    0x3A, 0x64, 0x86, 0xD8, 0x5B, 0x05, 0xE7, 0xB9,
    0x8C, 0xD2, 0x30, 0x6E, 0xED, 0xB3, 0x51, 0x0F,
    0x4E, 0x10, 0xF2, 0xAC, 0x2F, 0x71, 0x93, 0xCD,
    0x11, 0x4F, 0xAD, 0xF3, 0x70, 0x2E, 0xCC, 0x92,
    0xD3, 0x8D, 0x6F, 0x31, 0xB2, 0xEC, 0x0E, 0x50,
    0xAF, 0xF1, 0x13, 0x4D, 0xCE, 0x90, 0x72, 0x2C,
    0x6D, 0x33, 0xD1, 0x8F, 0x0C, 0x52, 0xB0, 0xEE,
    0x32, 0x6C, 0x8E, 0xD0, 0x53, 0x0D, 0xEF, 0xB1,
    0xF0, 0xAE, 0x4C, 0x12, 0x91, 0xCF, 0x2D, 0x73,
    0xCA, 0x94, 0x76, 0x28, 0xAB, 0xF5, 0x17, 0x49,
    0x08, 0x56, 0xB4, 0xEA, 0x69, 0x37, 0xD5, 0x8B,
    0x57, 0x09, 0xEB, 0xB5, 0x36, 0x68, 0x8A, 0xD4,
    0x95, 0xCB, 0x29, 0x77, 0xF4, 0xAA, 0x48, 0x16,
    0xE9, 0xB7, 0x55, 0x0B, 0x88, 0xD6, 0x34, 0x6A,
    0x2B, 0x75, 0x97, 0xC9, 0x4A, 0x14, 0xF6, 0xA8,
    0x74, 0x2A, 0xC8, 0x96, 0x15, 0x4B, 0xA9, 0xF7,
    0xB6, 0xE8, 0x0A, 0x54, 0xD7, 0x89, 0x6B, 0x35,
]
# fmt: on


def crc8(data: bytes) -> int:
    """CRC-8/MAXIM over data bytes."""
    crc = 0
    for b in data:
        crc = CRC8_TABLE[crc ^ b]
    return crc


def frame_checksum(frame: bytes) -> int:
    """Additive checksum: (256 - sum(bytes[1..N-1])) & 0xFF."""
    return (256 - sum(frame[1:-1])) & 0xFF


# ── Frame errors ──────────────────────────────────────────────────────────


class FrameError(Exception):
    """Invalid frame structure or integrity."""


# ── Core frame building / parsing ────────────────────────────────────────


def build_frame(
    body: bytes,
    msg_type: int,
    appliance: int = 0xAC,
    proto: int = 0x00,
    sub: int = 0x00,
    seq: int = 0x00,
) -> bytes:
    """Build a complete UART frame with header, body, CRC, and checksum."""
    frame_len = 9 + len(body) + 2  # bytes 1..N (header + body + CRC + CHK)
    sync = frame_len ^ appliance

    frame = bytearray()
    frame.append(0xAA)
    frame.append(frame_len)
    frame.append(appliance)
    frame.append(sync & 0xFF)
    frame.append(0x00)
    frame.append(0x00)
    frame.append(seq & 0xFF)
    frame.append(proto & 0xFF)
    frame.append(sub & 0xFF)
    frame.append(msg_type)
    frame.extend(body)
    frame.append(crc8(body))
    frame.append(0x00)
    frame[-1] = frame_checksum(frame)
    return bytes(frame)


def parse_frame(data: bytes) -> dict:
    """Parse and validate a UART frame.

    Returns dict with: msg_type, body, appliance, proto, sub, seq, crc_ok, checksum_ok.
    Raises FrameError on structural issues.
    """
    if len(data) < 13:
        raise FrameError(f"Frame too short: {len(data)} bytes (min 13)")
    if data[0] != 0xAA:
        raise FrameError(f"Invalid start byte: 0x{data[0]:02X} (expected 0xAA)")

    frame_len = data[1]
    expected_total = frame_len + 1
    if len(data) < expected_total:
        raise FrameError(f"Frame truncated: have {len(data)}, expected {expected_total}")

    appliance = data[2]
    seq = data[6]
    proto = data[7]
    sub = data[8]
    msg_type = data[9]
    body = data[10:-2]
    frame_crc = data[-2]
    frame_chk = data[-1]

    calc_crc = crc8(body)
    calc_chk = frame_checksum(data[:expected_total])

    return {
        "msg_type": msg_type,
        "body": bytes(body),
        "appliance": appliance,
        "proto": proto,
        "sub": sub,
        "seq": seq,
        "crc_ok": frame_crc == calc_crc,
        "checksum_ok": frame_chk == calc_chk,
    }


def validate_frame(data: bytes) -> dict:
    """Parse + validate, raising FrameError on CRC/checksum failure."""
    result = parse_frame(data)
    if not result["crc_ok"]:
        raise FrameError("CRC-8 mismatch")
    if not result["checksum_ok"]:
        raise FrameError("Checksum mismatch")
    return result


# ── Application-layer query builders ─────────────────────────────────────
#
# DEPRECATED: the bytes these builders emit now live in the top-level
# `frames:` dict in serial_glossary.yaml. Prefer
# `midea_codec.build_frame_from_spec(frame_id, glossary)` — it reads the
# body specs directly from the glossary, enforces the bus filter, and
# passes the test_frames_dict invariants. These wrappers are kept for
# backward compatibility with ac_monitor.py and any external callers;
# removing them is tracked in TODO §6 phase 2 final sweep.
#
# The wrappers must remain byte-identical to the glossary output so that
# the old behaviour is preserved for the C0 / B5 / group1 / group3
# callers. Groups 4 and 5 are the documented exception: before the
# glossary landing, `build_group_query(page=0x44)` emitted body[1]=0x81
# — never observed in any capture. The glossary now carries the
# capture-correct body[1]=0x21 for those pages, so the wrapper's output
# for page=0x44/0x45 intentionally differs from the pre-phase-2 baseline.
# The change fixes the runtime bug documented in
# serial_glossary.yaml::frames.cmd_0x41_group4_power::note.


def _load_frames_lazy():
    """Import from midea_codec lazily to avoid an import cycle at module load."""
    from blaueis.core.codec import load_glossary

    return load_glossary()


def build_status_query(appliance: int = 0xAC, proto: int = 0, sub: int = 0) -> bytes:
    """CMD 0x41 — Status query (triggers 0xC0 response).

    DEPRECATED: prefer build_frame_from_spec("cmd_0x41", glossary).
    Returns power, mode, fan, target_temp, indoor/outdoor temp, error code.
    Works on UART and R/T bus.
    """
    from blaueis.core.codec import build_frame_from_spec

    return build_frame_from_spec(
        "cmd_0x41",
        _load_frames_lazy(),
        appliance=appliance,
        proto=proto,
        sub=sub,
    )


# Map legacy page byte → frames dict ID. The page selector used to live in
# `build_group_query`; after phase 2 it's just a routing table from the
# single integer argument to the canonical frame ID. Groups 4 and 5 route
# to the UART-specific power-query variant (body[1]=0x21).
_GROUP_PAGE_TO_FRAME_ID = {
    0x40: "cmd_0x41_group0",
    0x41: "cmd_0x41_group1",
    0x42: "cmd_0x41_group2",
    0x43: "cmd_0x41_group3",
    0x44: "cmd_0x41_group4_power",
    0x45: "cmd_0x41_group5",
    0x46: "cmd_0x41_group6",
    0x47: "cmd_0x41_group7",
    0x4B: "cmd_0x41_group11",
    0x4C: "cmd_0x41_group12",
}


def build_group_query(appliance: int = 0xAC, page: int = 0x41, proto: int = 0, sub: int = 0) -> bytes:
    """CMD 0x41 group page query (triggers C1 group response).

    DEPRECATED: prefer build_frame_from_spec("cmd_0x41_groupN", glossary).

    Page IDs: 0x41=Group1, 0x43=Group3 (both R/T bus only);
    0x44=Group4 power, 0x45=Group5 (both UART bus only). See
    serial_protocol.md §3.1.2 and the frames dict in
    serial_glossary.yaml for the correct body bytes per bus.
    """
    from blaueis.core.codec import build_frame_from_spec

    if page not in _GROUP_PAGE_TO_FRAME_ID:
        raise ValueError(
            f"unsupported group page 0x{page:02X} — known pages: {[hex(p) for p in _GROUP_PAGE_TO_FRAME_ID]}"
        )
    return build_frame_from_spec(
        _GROUP_PAGE_TO_FRAME_ID[page],
        _load_frames_lazy(),
        appliance=appliance,
        proto=proto,
        sub=sub,
    )


def build_cap_query_extended(appliance: int = 0xAC, proto: int = 0, sub: int = 0) -> bytes:
    """CMD 0xB5 — Extended capability query (page 0x00).

    DEPRECATED: prefer build_frame_from_spec("cmd_0xb5_extended", glossary).
    Returns extended-type (0x02) capability records: encoding-rich features
    like fan modes, operating modes, target temperature ranges, power calc.
    """
    from blaueis.core.codec import build_frame_from_spec

    return build_frame_from_spec(
        "cmd_0xb5_extended",
        _load_frames_lazy(),
        appliance=appliance,
        proto=proto,
        sub=sub,
    )


def build_cap_query_simple(appliance: int = 0xAC, proto: int = 0, sub: int = 0) -> bytes:
    """CMD 0xB5 — Simple capability query (page 0x01 with extra bytes).

    DEPRECATED: prefer build_frame_from_spec("cmd_0xb5_simple", glossary).
    Returns simple-type (0x00) capability records: boolean features like
    swing axes, frost protection, anion ionizer, breeze, self-clean, ptc heater.
    """
    from blaueis.core.codec import build_frame_from_spec

    return build_frame_from_spec(
        "cmd_0xb5_simple",
        _load_frames_lazy(),
        appliance=appliance,
        proto=proto,
        sub=sub,
    )


# ── B1 property query ──────────────────────────────────────────────────


def build_b1_property_query(
    prop_ids,
    appliance: int = 0xAC,
    proto: int = 0,
    sub: int = 0,
) -> bytes:
    """CMD 0xB1 — property query (triggers 0xB1 response).

    `prop_ids` is an iterable of ``(lo, hi)`` byte pairs identifying the
    properties to read. Body layout: ``0xB1, count, [(lo, hi) × count]``.
    The AC replies with a 0xB1 frame containing, per prop, ``(lo, hi, dl,
    data…)``; a device that does not implement a property returns
    ``dl == 0`` for it rather than dropping the id.

    The caller is responsible for keeping the total body under the
    length-byte ceiling (250 bytes → ~120 prop_ids max). Practical batch
    sizes used elsewhere in the workspace: 8–16 per frame.
    """
    pairs = list(prop_ids)
    if not pairs:
        raise ValueError("prop_ids must not be empty")
    body = bytearray()
    body.append(0xB1)
    body.append(len(pairs))
    for lo, hi in pairs:
        body.append(lo & 0xFF)
        body.append(hi & 0xFF)
    return build_frame(
        body=bytes(body),
        msg_type=0x03,
        appliance=appliance,
        proto=proto,
        sub=sub,
    )


# ── Follow Me temperature frame ───────────────────────────────────────


def build_follow_me_frame(
    celsius: float,
    appliance: int = 0xAC,
    proto: int = 0x00,
    sub: int = 0x00,
) -> bytes:
    """CMD 0x41 optCommand=0x01 — Send Follow Me temperature.

    The AC uses this value instead of its built-in thermistor.
    Must be re-sent periodically or the AC reverts (~60s timeout).
    Encoding: body[5] = T*2+50. Clamps to [0.0, 50.0]°C.
    """
    celsius = max(0.0, min(50.0, float(celsius)))
    raw = int(round(celsius * 2 + 50))
    raw = max(0, min(255, raw))
    body = bytearray(24)
    body[0] = 0x41
    body[4] = 0x01
    body[5] = raw
    return build_frame(
        body=bytes(body),
        msg_type=0x03,
        appliance=appliance,
        proto=proto,
        sub=sub,
    )


# ── Gateway handshake frame builders ───────────────────────────────────
#
# These build the UART-level handshake frames the gateway sends to the AC
# during dongle impersonation (DISCOVER → MODEL → ANNOUNCE → RUNNING).
# They are NOT in the glossary because they're gateway-originated, not
# data queries. The byte layouts match observed real-dongle traffic.


def build_sn_query(appliance: int = 0xFF, proto: int = 0, sub: int = 0) -> bytes:
    """MSG 0x07 — Serial number / device ID query.

    Sent during DISCOVER with appliance=0xFF (broadcast) to find any AC
    on the bus. AC responds with its SN in the body (ASCII, zero-padded).
    """
    return build_frame(bytes(20), msg_type=0x07, appliance=appliance, proto=proto, sub=sub)


def build_model_query(appliance: int = 0xAC, proto: int = 0, sub: int = 0) -> bytes:
    """MSG 0xA0 — Model number query.

    Sent during MODEL phase. AC responds with model ID in body[2:4] (LE uint16).
    """
    return build_frame(bytes(20), msg_type=0xA0, appliance=appliance, proto=proto, sub=sub)


def build_network_init(
    appliance: int = 0xAC,
    ip: tuple[int, int, int, int] = (192, 168, 1, 100),
    proto: int = 0,
    sub: int = 0,
) -> bytes:
    """MSG 0x0D — Network init (dongle announces its IP to the AC).

    Sent during ANNOUNCE after SN + model are known. Tells the AC the
    dongle is online and ready. The IP is embedded in body[3:7].
    """
    body = bytearray(20)
    body[3] = ip[0]
    body[4] = ip[1]
    body[5] = ip[2]
    body[6] = ip[3]
    return build_frame(bytes(body), msg_type=0x0D, appliance=appliance, proto=proto, sub=sub)


def build_network_status_response(
    ip: tuple[int, int, int, int] = (192, 168, 1, 100),
    signal: int = 4,
    connected: bool = True,
) -> bytes:
    """Build the body for a MSG 0x63 network status response.

    Returns the body only — caller wraps it with build_frame(body, msg_type=0x63, ...).
    The AC sends 0x63 queries periodically (~60s) to check connectivity.

    Body layout:
      [0]    = connection status (0x11 connected, 0x00 disconnected)
      [2:6]  = IP address bytes
      [8]    = signal strength (0–4)
    """
    body = bytearray(20)
    body[0] = 0x11 if connected else 0x00
    body[2] = ip[0]
    body[3] = ip[1]
    body[4] = ip[2]
    body[5] = ip[3]
    body[8] = signal & 0xFF
    return bytes(body)


def build_version_response(appliance: int = 0xAC, proto: int = 0, sub: int = 0) -> bytes:
    """MSG 0x13 — Version info response.

    Sent when the AC queries firmware version (0x13 or 0x87).
    Returns a plausible dongle version string.
    """
    # Minimal version body: "V1.0.0" zero-padded
    version = b"V1.0.0"
    body = bytearray(20)
    body[:len(version)] = version
    return build_frame(bytes(body), msg_type=0x13, appliance=appliance, proto=proto, sub=sub)
