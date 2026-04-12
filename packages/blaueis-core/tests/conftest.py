"""Test configuration for blaueis-core.

The package is installed in editable mode (`pip install -e .`), so
`from blaueis.core.codec import load_glossary` works directly.
No sys.path hacks needed.

Legacy script-style tests (module-level execution + sys.exit) are
excluded from pytest collection. Run them standalone:
    python packages/blaueis-core/tests/test_frame.py
"""

# Script-style tests that use sys.exit() at module level.
# pytest can't collect these — they run on import and call sys.exit().
# New tests should use def test_*() functions for pytest compatibility.
collect_ignore = [
    "test_apply_device_quirks.py",
    "test_b0b1_bulk_properties.py",
    "test_b0b1_property.py",
    "test_build_status.py",
    "test_capture_replay.py",
    "test_category_boundary.py",
    "test_command_builder.py",
    "test_crypto.py",
    "test_default_constraints.py",
    "test_dissector_gen.py",
    "test_field_query.py",
    "test_formula_evaluator.py",
    "test_frame.py",
    "test_frames_dict.py",
    "test_glossary_invariants.py",
    "test_multibyte_encoding.py",
    "test_pipeline.py",
    "test_process_frame.py",
    "test_scan_queue.py",
    "test_schema_validation.py",
    "test_set_command_preflight.py",
    "validate_complex_fields.py",
]
