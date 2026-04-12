#!/usr/bin/env python3
"""Tests for the blaueis-configure wizard — core functions only.

No interactive prompts tested (those need manual/integration testing).
Validates: PSK generation, PSK validation, key derivation, instance
name validation, config file writing, port validation.

Usage:
    python test_configure.py
"""

import hashlib
import string

# Add scripts dir to path so we can import from blaueis-configure
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))

import blaueis_configure as wizard  # noqa: E402

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}: {detail}")


def main():
    # ── PSK generation ──────────────────────────────────
    print("\n1. PSK generation")

    psk = wizard.generate_psk()
    check("generated PSK length == 44", len(psk) == 44, f"got {len(psk)}")
    check(
        "generated PSK is alphanumeric only",
        all(c in string.ascii_letters + string.digits for c in psk),
        f"got non-alnum chars: {[c for c in psk if c not in string.ascii_letters + string.digits]}",
    )

    # Two calls produce different keys (randomness)
    psk2 = wizard.generate_psk()
    check("two generated PSKs differ", psk != psk2, "identical — bad RNG?")

    # ── PSK validation ──────────────────────────────────
    print("\n2. PSK validation")

    check("12-char PSK accepted", wizard.validate_psk("abcdefghijkl") == "abcdefghijkl")
    check("44-char PSK accepted", wizard.validate_psk(psk) == psk)
    check("100-char PSK accepted", wizard.validate_psk("a" * 100) == "a" * 100)

    try:
        wizard.validate_psk("short")
        check("5-char PSK rejected", False, "no exception raised")
    except ValueError:
        check("5-char PSK rejected", True)

    try:
        wizard.validate_psk("")
        check("empty PSK rejected", False, "no exception raised")
    except ValueError:
        check("empty PSK rejected", True)

    try:
        wizard.validate_psk("12345678901")  # 11 chars
        check("11-char PSK rejected", False, "no exception raised")
    except ValueError:
        check("11-char PSK rejected", True)

    # ── Key derivation ──────────────────────────────────
    print("\n3. PSK to AES key derivation")

    key = wizard.psk_to_key("testpassphrase123")
    check("key is 32 bytes", len(key) == 32, f"got {len(key)}")
    check(
        "key matches SHA-256",
        key == hashlib.sha256(b"testpassphrase123").digest(),
    )

    # Same input → same key (deterministic)
    key2 = wizard.psk_to_key("testpassphrase123")
    check("same input → same key", key == key2)

    # Different input → different key
    key3 = wizard.psk_to_key("differentpassphrase")
    check("different input → different key", key != key3)

    # ── Instance name validation ─────────────────────────
    print("\n4. Instance name validation")

    pattern = wizard.INSTANCE_NAME_RE
    check("'living-room' valid", pattern.match("living-room") is not None)
    check("'bedroom' valid", pattern.match("bedroom") is not None)
    check("'ac1' valid", pattern.match("ac1") is not None)
    check("'a' valid (single char)", pattern.match("a") is not None)
    check("'unit-2f' valid", pattern.match("unit-2f") is not None)

    check("'Living-Room' invalid (uppercase)", pattern.match("Living-Room") is None)
    check("'-start' invalid (leading hyphen)", pattern.match("-start") is None)
    check("'end-' invalid (trailing hyphen)", pattern.match("end-") is None)
    check("'has space' invalid", pattern.match("has space") is None)
    check("'has_underscore' invalid", pattern.match("has_underscore") is None)
    check("'' invalid (empty)", pattern.match("") is None)

    # ── Port validation ──────────────────────────────────
    print("\n5. Port validation")

    check("8765 valid", wizard._validate_port("8765") == "8765")
    check("1024 valid (min)", wizard._validate_port("1024") == "1024")
    check("65535 valid (max)", wizard._validate_port("65535") == "65535")

    try:
        wizard._validate_port("80")
        check("80 rejected (< 1024)", False, "no exception")
    except ValueError:
        check("80 rejected (< 1024)", True)

    try:
        wizard._validate_port("99999")
        check("99999 rejected (> 65535)", False, "no exception")
    except ValueError:
        check("99999 rejected (> 65535)", True)

    # ── Config writing ───────────────────────────────────
    print("\n6. Config file writing")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Monkey-patch the paths for testing
        orig_instances = wizard.INSTANCES_DIR
        orig_global = wizard.GLOBAL_CONFIG
        wizard.INSTANCES_DIR = Path(tmpdir) / "instances"
        wizard.GLOBAL_CONFIG = Path(tmpdir) / "gateway.yaml"

        try:
            # Write global
            wizard.write_global_config()
            check("global config created", wizard.GLOBAL_CONFIG.exists())
            global_content = wizard.GLOBAL_CONFIG.read_text()
            check("global has schema_version", "schema_version: 1" in global_content)
            check("global has remote_management", "remote_management:" in global_content)

            # Write global again — should not overwrite
            wizard.GLOBAL_CONFIG.write_text("# custom\n")
            wizard.write_global_config()
            check("global not overwritten on second call", wizard.GLOBAL_CONFIG.read_text() == "# custom\n")

            # Write instance
            path = wizard.write_instance_config(
                "test-ac", "/dev/serial0", 9600, "8765", "myTestKey12345678", "Test AC", "192.168.1.50"
            )
            check("instance config created", path.exists())
            inst_content = path.read_text()
            check("instance has schema_version", "schema_version: 1" in inst_content)
            check("instance has serial_port", "serial_port: /dev/serial0" in inst_content)
            check("instance has PSK", "myTestKey12345678" in inst_content)
            check("instance has HA block", "Host: 192.168.1.50" in inst_content)
            check("instance has port in HA block", "Port: 8765" in inst_content)
            check("instance has device name", "name: Test AC" in inst_content)
        finally:
            wizard.INSTANCES_DIR = orig_instances
            wizard.GLOBAL_CONFIG = orig_global

    # ── Summary ──────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
