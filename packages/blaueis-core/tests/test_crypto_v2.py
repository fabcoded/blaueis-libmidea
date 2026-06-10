"""Session-protocol v2 invariants: direction separation, key
confirmation failure modes, KDF stretching, version gating.

These pin the three v2 security properties:
  1. The two channel directions can never collide on a (key, nonce) pair
     even though both sides count from 0.
  2. A PSK mismatch is detectable on the very first decrypt (InvalidTag),
     which the client's connect()-time key confirmation relies on.
  3. Passphrases are stretched (scrypt), not single-hashed.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from cryptography.exceptions import InvalidTag

from blaueis.core.crypto import (
    PROTOCOL_VERSION,
    HandshakeError,
    ReplayError,
    complete_handshake_client,
    complete_handshake_server,
    create_hello,
    create_hello_ok,
    derive_session,
    generate_psk,
    psk_to_bytes,
)


def _pair(psk: bytes | None = None):
    """Derive a matched client/server session pair via the real handshake."""
    psk = psk or generate_psk()
    hello, client_rand = create_hello()
    hello_ok, server_rand = create_hello_ok()
    server = complete_handshake_server(psk, hello, server_rand)
    client = complete_handshake_client(psk, client_rand, hello_ok)
    return client, server


def test_round_trip_both_directions():
    client, server = _pair()
    assert server.decrypt(client.encrypt(b"to-server")) == b"to-server"
    assert client.decrypt(server.encrypt(b"to-client")) == b"to-client"


def test_directions_never_share_key_and_nonce():
    """Same counter + same plaintext must yield different ciphertexts per
    direction — the v1 bug was identical (key, nonce) on both sides."""
    client, server = _pair()
    env_c = client.encrypt(b"x")  # c2s, counter 0
    env_s = server.encrypt(b"x")  # s2c, counter 0
    assert env_c["c"] == env_s["c"] == 0
    assert env_c["ct"] != env_s["ct"] or env_c["tag"] != env_s["tag"]
    # And a side can never decrypt its own transmission (different key).
    with pytest.raises(InvalidTag):
        client.decrypt(client.encrypt(b"echo"))


def test_wrong_psk_fails_on_first_decrypt():
    """Key confirmation depends on this: the first cross-direction decrypt
    with a mismatched PSK raises InvalidTag."""
    psk_good, psk_bad = generate_psk(), generate_psk()
    hello, client_rand = create_hello()
    hello_ok, server_rand = create_hello_ok()
    server = complete_handshake_server(psk_good, hello, server_rand)
    client = complete_handshake_client(psk_bad, client_rand, hello_ok)
    slot_hello = server.encrypt(json.dumps({"type": "hello", "sid": 1}).encode())
    with pytest.raises(InvalidTag):
        client.decrypt(slot_hello)


def test_v1_hello_is_refused():
    assert PROTOCOL_VERSION == 2
    hello, _ = create_hello()
    hello["version"] = 1
    _, server_rand = create_hello_ok()
    with pytest.raises(HandshakeError, match="version"):
        complete_handshake_server(generate_psk(), hello, server_rand)


def test_replay_protection_still_enforced():
    client, server = _pair()
    env = client.encrypt(b"once")
    assert server.decrypt(env) == b"once"
    with pytest.raises(ReplayError):
        server.decrypt(env)


def test_psk_stretching_is_scrypt_not_sha256():
    passphrase = "mySecretKey123"
    stretched = psk_to_bytes(passphrase)
    assert len(stretched) == 32
    assert stretched != hashlib.sha256(passphrase.encode()).digest()
    # Deterministic — both ends must derive the identical key.
    assert stretched == psk_to_bytes(passphrase)
    assert stretched != psk_to_bytes("mySecretKey124")


def test_derive_session_rejects_unknown_role():
    with pytest.raises(ValueError, match="role"):
        derive_session(generate_psk(), b"a" * 512, b"b" * 512, role="peer")
