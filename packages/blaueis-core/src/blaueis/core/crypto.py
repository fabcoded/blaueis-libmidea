"""AES-256-GCM session crypto for HVAC gateway WebSocket channel.

PSK-based session establishment with HKDF key derivation and
monotonic counter replay protection. Optional — can be disabled
with --no-encrypt for development.

Session handshake (plaintext WebSocket, before encryption):
  Client → Gateway:  {"type": "hello", "version": 2, "client_rand": "<base64 512B>"}
  Gateway → Client:  {"type": "hello_ok", "server_rand": "<base64 512B>"}

Protocol v2 (2026-06): each direction derives its own session key and
nonce prefix (HKDF info labels), so the two directions can never collide
on a (key, nonce) pair; passphrases are stretched with scrypt instead of
a single SHA-256; and the client confirms the key by decrypting the
gateway's first encrypted message before declaring the session up.
v1 peers are refused with a clean version-mismatch handshake error.

After handshake, all messages are encrypted envelopes (unchanged from v1):
  {"c": <counter>, "ct": "<base64 ciphertext>", "tag": "<base64 tag>"}
"""

import base64
import json
import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

PROTOCOL_VERSION = 2
RAND_SIZE = 512  # bytes per side
SESSION_INFO_C2S = b"blaueis-session-c2s-v2"
SESSION_INFO_S2C = b"blaueis-session-s2c-v2"
NONCE_INFO = b"blaueis-nonce-v2"
# Static application-context salt for passphrase stretching. The
# per-session salt remains the handshake rands fed into HKDF; this one
# only binds the stretched key to this protocol so identical passphrases
# in other systems don't yield the same key material.
PSK_SCRYPT_SALT = b"blaueis-gateway-psk-v2"


class ReplayError(Exception):
    """Received message with non-monotonic counter."""


class HandshakeError(Exception):
    """Session handshake failed.

    Base class for any connect-time failure. Without further
    classification this is a *transient* condition (slot pool full,
    malformed reply, half-open close) — consumers retry. Credential
    rejections raise the :class:`AuthenticationError` subclass instead.
    """


class AuthenticationError(HandshakeError):
    """Credential rejection, confirmed cryptographically.

    Raised only when key confirmation fails (the gateway's first
    encrypted message does not decrypt under our PSK-derived session
    key). Retrying cannot fix it — consumers should stop and ask for a
    new key. A protocol-version refusal never reaches key confirmation:
    the server closes during the handshake, which consumers see as an
    ordinary connection error and keep retrying until both sides are
    updated together.
    """


class Session:
    """Encrypted session state for one WebSocket connection.

    Direction-separated (v2): the transmit and receive directions use
    distinct keys and nonce prefixes, so both sides counting from 0 can
    never produce a (key, nonce) collision across directions.
    """

    def __init__(
        self,
        tx_key: bytes,
        tx_nonce_prefix: bytes,
        rx_key: bytes,
        rx_nonce_prefix: bytes,
    ):
        self.tx_nonce_prefix = tx_nonce_prefix  # 4 bytes
        self.rx_nonce_prefix = rx_nonce_prefix  # 4 bytes
        self.tx_counter = 0
        self.rx_counter = -1  # accept counter >= 0
        self._gcm_tx = AESGCM(tx_key)
        self._gcm_rx = AESGCM(rx_key)

    def encrypt(self, plaintext: bytes) -> dict:
        """Encrypt plaintext, return envelope dict for JSON serialization."""
        counter = self.tx_counter
        self.tx_counter += 1
        nonce = self.tx_nonce_prefix + struct.pack(">Q", counter)  # 4 + 8 = 12 bytes
        ct = self._gcm_tx.encrypt(nonce, plaintext, None)
        # GCM appends 16-byte tag to ciphertext
        ciphertext = ct[:-16]
        tag = ct[-16:]
        return {
            "c": counter,
            "ct": base64.b64encode(ciphertext).decode(),
            "tag": base64.b64encode(tag).decode(),
        }

    def decrypt(self, envelope: dict) -> bytes:
        """Decrypt envelope dict, validate counter for replay protection."""
        counter = envelope["c"]
        if counter <= self.rx_counter:
            raise ReplayError(f"Counter {counter} <= last seen {self.rx_counter}")
        self.rx_counter = counter
        nonce = self.rx_nonce_prefix + struct.pack(">Q", counter)
        ct = base64.b64decode(envelope["ct"])
        tag = base64.b64decode(envelope["tag"])
        return self._gcm_rx.decrypt(nonce, ct + tag, None)

    def encrypt_json(self, obj: dict) -> str:
        """Encrypt a JSON-serializable dict, return JSON envelope string."""
        plaintext = json.dumps(obj).encode()
        return json.dumps(self.encrypt(plaintext))

    def decrypt_json(self, envelope_str: str) -> dict:
        """Decrypt a JSON envelope string, return parsed dict."""
        envelope = json.loads(envelope_str)
        plaintext = self.decrypt(envelope)
        return json.loads(plaintext)


def _derive_direction(psk: bytes, salt: bytes, info: bytes) -> tuple[bytes, bytes]:
    """Derive one direction's (key, nonce_prefix) pair."""
    key = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=salt,
        info=info,
    ).derive(psk)
    nonce_prefix = HKDF(
        algorithm=SHA256(),
        length=4,
        salt=None,
        info=NONCE_INFO,
    ).derive(key)
    return key, nonce_prefix


def derive_session(
    psk: bytes, client_rand: bytes, server_rand: bytes, role: str
) -> Session:
    """Derive the direction-separated session for one side.

    ``role`` is ``"client"`` or ``"server"``: both sides derive the same
    two (key, nonce_prefix) pairs from the shared material, then map
    c2s/s2c onto their own tx/rx according to which end they are.
    """
    salt = client_rand + server_rand  # 1024 bytes
    c2s = _derive_direction(psk, salt, SESSION_INFO_C2S)
    s2c = _derive_direction(psk, salt, SESSION_INFO_S2C)
    if role == "client":
        tx, rx = c2s, s2c
    elif role == "server":
        tx, rx = s2c, c2s
    else:
        raise ValueError(f"role must be 'client' or 'server', got {role!r}")
    return Session(tx[0], tx[1], rx[0], rx[1])


# ── Handshake helpers ─────────────────────────────────────────────────────


def create_hello(client_rand: bytes | None = None) -> tuple[dict, bytes]:
    """Create client hello message. Returns (message_dict, client_rand)."""
    if client_rand is None:
        client_rand = os.urandom(RAND_SIZE)
    return {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "client_rand": base64.b64encode(client_rand).decode(),
    }, client_rand


def create_hello_ok(server_rand: bytes | None = None) -> tuple[dict, bytes]:
    """Create server hello_ok message. Returns (message_dict, server_rand)."""
    if server_rand is None:
        server_rand = os.urandom(RAND_SIZE)
    return {
        "type": "hello_ok",
        "server_rand": base64.b64encode(server_rand).decode(),
    }, server_rand


def complete_handshake_client(psk: bytes, client_rand: bytes, hello_ok: dict) -> Session:
    """Client side: complete handshake after receiving hello_ok."""
    if hello_ok.get("type") != "hello_ok":
        raise HandshakeError(f"Expected hello_ok, got {hello_ok.get('type')}")
    server_rand = base64.b64decode(hello_ok["server_rand"])
    if len(server_rand) != RAND_SIZE:
        raise HandshakeError(f"Invalid server_rand size: {len(server_rand)}")
    return derive_session(psk, client_rand, server_rand, role="client")


def complete_handshake_server(psk: bytes, hello: dict, server_rand: bytes) -> Session:
    """Server side: complete handshake after receiving hello."""
    if hello.get("type") != "hello":
        raise HandshakeError(f"Expected hello, got {hello.get('type')}")
    if hello.get("version") != PROTOCOL_VERSION:
        raise HandshakeError(f"Protocol version mismatch: {hello.get('version')}")
    client_rand = base64.b64decode(hello["client_rand"])
    if len(client_rand) != RAND_SIZE:
        raise HandshakeError(f"Invalid client_rand size: {len(client_rand)}")
    return derive_session(psk, client_rand, server_rand, role="server")


# ── PSK management ────────────────────────────────────────────────────────


def generate_psk() -> bytes:
    """Generate a new 32-byte PSK."""
    return os.urandom(32)


def psk_to_bytes(psk_str: str) -> bytes:
    """Stretch a passphrase into 32 raw bytes for the AES-256 handshake.

    v2: scrypt (n=2^14, r=8, p=1, ~16 MiB) with a static application
    context salt, so a leaked handshake transcript no longer enables a
    cheap offline dictionary attack on memorable passphrases. Runs once
    per process (the result is cached by callers), Pi-friendly. Both
    ends MUST use this same function — it is the shared key derivation.
    """
    psk_str = psk_str.strip()
    if not psk_str:
        raise ValueError("PSK is empty — configure encryption or use --no-encrypt")
    kdf = Scrypt(salt=PSK_SCRYPT_SALT, length=32, n=2**14, r=8, p=1)
    return kdf.derive(psk_str.encode("utf-8"))


def load_psk(config_path: str) -> bytes:
    """Load PSK from a config file. Expects a line: psk = <hex>."""
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    psk_hex = cfg.get("gateway", "psk", fallback=None)
    if not psk_hex:
        raise ValueError(f"No PSK found in {config_path}")
    return bytes.fromhex(psk_hex.strip())
