"""The names the HA integration imports are declared in each module's ``__all__``."""

import importlib

import pytest

CONSUMED = {
    "blaueis.core.codec": ["load_glossary", "load_glossary_schema", "invalidate_glossary_cache", "walk_fields"],
    "blaueis.core.command": ["build_b0_command_body", "build_command_body"],
    "blaueis.core.crypto": ["AuthenticationError", "HandshakeError", "psk_to_bytes"],
    "blaueis.core.debug_ring": ["DebugRing", "log_event"],
    "blaueis.core.formula": ["FormulaError", "evaluate"],
    "blaueis.core.frame": ["build_frame", "parse_frame", "crc8"],
    "blaueis.core.gate_eval": ["GateVerdict", "evaluate_offered"],
    "blaueis.core.glossary_override": ["OverrideMessage", "apply_override"],
    "blaueis.core.inventory": ["ShadowDecoder", "generate_markdown_report"],
    "blaueis.core.process": ["process_b5", "process_data_frame", "finalize_capabilities"],
    "blaueis.core.query": ["read_field", "write_field"],
    "blaueis.core.quirks": ["apply_device_quirks", "load_device_quirks"],
    "blaueis.core.scan_queries": ["build_scan_queries"],
    "blaueis.core.status": ["build_status"],
    "blaueis.core.ux_gating": ["MODE_INT_TO_NAME", "is_field_visible"],
    "blaueis.core.validation": ["validate_set", "Ok"],
}


@pytest.mark.parametrize("module", sorted(CONSUMED))
def test_consumed_names_are_declared_and_exist(module):
    mod = importlib.import_module(module)
    declared = set(mod.__all__)
    assert set(CONSUMED[module]) <= declared
    for name in declared:
        assert hasattr(mod, name), f"{module}.__all__ lists {name!r} but the module has no such attribute"


def test_load_glossary_schema_is_cached_and_a_json_schema():
    from blaueis.core.codec import load_glossary_schema

    schema = load_glossary_schema()
    assert schema is load_glossary_schema()
    assert schema.get("$schema")
