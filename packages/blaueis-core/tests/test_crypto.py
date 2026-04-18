#!/usr/bin/env python3
"""Tests for hvac_crypto.py — AES-256-GCM session, handshake, replay protection.

Usage:
    python test_crypto.py
"""

import sys

from blaueis.core.crypto import (
    HandshakeError,
    ReplayError,
    complete_handshake_client,
    complete_handshake_server,
    create_hello,
    create_hello_ok,
    derive_session,
    generate_psk,
)


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
        else:
            failed += 1
            print(f"  [FAIL] {name}: {detail}")

    psk = generate_psk()
    check("PSK is 32 bytes", len(psk) == 32)

    # ── Handshake ────────────────────────────────────────────────
    hello_msg, client_rand = create_hello()
    check("hello type=hello", hello_msg["type"] == "hello")
    check("hello has client_rand", "client_rand" in hello_msg)
    check("hello version=1", hello_msg["version"] == 1)

    hello_ok_msg, server_rand = create_hello_ok()
    check("hello_ok type=hello_ok", hello_ok_msg["type"] == "hello_ok")

    # Both sides derive session
    client_session = complete_handshake_client(psk, client_rand, hello_ok_msg)
    server_session = complete_handshake_server(psk, hello_msg, server_rand)

    check("sessions have same key", client_session.key == server_session.key)
    check("sessions have same nonce_prefix", client_session.nonce_prefix == server_session.nonce_prefix)
    check("key is 32 bytes", len(client_session.key) == 32)
    check("nonce_prefix is 4 bytes", len(client_session.nonce_prefix) == 4)

    # ── Encrypt/decrypt round-trip ───────────────────────────────
    plaintext = b"Hello HVAC Shark!"
    envelope = client_session.encrypt(plaintext)

    check("envelope has counter", "c" in envelope)
    check("envelope has ciphertext", "ct" in envelope)
    check("envelope has tag", "tag" in envelope)
    check("counter starts at 0", envelope["c"] == 0)

    decrypted = server_session.decrypt(envelope)
    check("round-trip plaintext matches", decrypted == plaintext)

    # Second message
    env2 = client_session.encrypt(b"message two")
    check("counter increments", env2["c"] == 1)
    dec2 = server_session.decrypt(env2)
    check("second message decrypts", dec2 == b"message two")

    # ── JSON convenience ─────────────────────────────────────────
    test_obj = {"type": "frame", "hex": "AA 23 AC", "ref": 42}
    encrypted_str = client_session.encrypt_json(test_obj)
    decrypted_obj = server_session.decrypt_json(encrypted_str)
    check("JSON round-trip type", decrypted_obj["type"] == "frame")
    check("JSON round-trip ref", decrypted_obj["ref"] == 42)

    # ── Replay protection ────────────────────────────────────────
    # Re-send envelope with counter=0 (already seen)
    try:
        server_session.decrypt(envelope)
        check("replay rejected", False, "no exception raised")
    except ReplayError:
        check("replay rejected", True)

    # ── Tampered ciphertext ──────────────────────────────────────
    env3 = client_session.encrypt(b"tamper test")
    env3_tampered = dict(env3)
    import base64

    ct_bytes = bytearray(base64.b64decode(env3_tampered["ct"]))
    if ct_bytes:
        ct_bytes[0] ^= 0xFF
    env3_tampered["ct"] = base64.b64encode(bytes(ct_bytes)).decode()

    try:
        server_session.decrypt(env3_tampered)
        check("tamper detected", False, "no exception raised")
    except Exception:
        check("tamper detected", True)

    # ── Reverse direction (server → client) ──────────────────────
    server_env = server_session.encrypt(b"from server")
    # Client needs a fresh session for reverse direction since counters are independent
    # Actually both sessions share the same key, so client can decrypt server messages
    # But rx_counter tracks separately
    client_dec = client_session.decrypt(server_env)
    check("reverse direction works", client_dec == b"from server")

    # ── Deterministic key derivation ─────────────────────────────
    # Same inputs → same key
    s1 = derive_session(psk, client_rand, server_rand)
    s2 = derive_session(psk, client_rand, server_rand)
    check("deterministic key", s1.key == s2.key)
    check("deterministic nonce", s1.nonce_prefix == s2.nonce_prefix)

    # Different PSK → different key
    psk2 = generate_psk()
    s3 = derive_session(psk2, client_rand, server_rand)
    check("different PSK → different key", s3.key != s1.key)

    # ── Handshake validation ─────────────────────────────────────
    try:
        complete_handshake_client(psk, client_rand, {"type": "wrong"})
        check("bad hello_ok rejected", False)
    except HandshakeError:
        check("bad hello_ok rejected", True)

    try:
        complete_handshake_server(psk, {"type": "wrong"}, server_rand)
        check("bad hello rejected", False)
    except HandshakeError:
        check("bad hello rejected", True)

    # ── Summary ──────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
