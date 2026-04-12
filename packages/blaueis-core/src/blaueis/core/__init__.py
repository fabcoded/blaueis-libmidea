"""blaueis.core — Midea HVAC serial protocol library.

Pure sync Python. No I/O, no async, no transport. Glossary-driven
codec, state management, command building, and field query API.

Public API:

    from blaueis.core.codec import load_glossary, decode_frame_fields, encode_field
    from blaueis.core.status import build_status
    from blaueis.core.process import process_raw_frame, finalize_capabilities
    from blaueis.core.query import read_field
    from blaueis.core.command import build_command_body, build_b0_command_body
    from blaueis.core.quirks import apply_device_quirks, load_device_quirks
    from blaueis.core.frame import build_frame, parse_frame
    from blaueis.core.codec import identify_frame, infer_generation
"""
