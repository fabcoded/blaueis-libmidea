"""AES-256-GCM session crypto for HVAC gateway WebSocket channel.

PSK-based session establishment with HKDF key derivation and
monotonic counter replay protection. Optional — can be disabled
with --no-encrypt for development.

Session handshake (plaintext WebSocket, before encryption):
  Client → Gateway:  {"type": "hello", "version": 1, "client_rand": "<base64 512B>"}
  Gateway → Client:  {"type": "hello_ok", "server_rand": "<base64 512B>"}

After handshake, all messages are encrypted envelopes:
  {"c": <counter>, "ct": "<base64 ciphertext>", "tag": "<base64 tag>"}
"""

import base64
import json
import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL_VERSION = 1
RAND_SIZE = 512  # bytes per side
SESSION_INFO = b"hvac-shark-session-v1"
NONCE_INFO = b"hvac-shark-nonce-v1"


class ReplayError(Exception):
    """Received message with non-monotonic counter."""


class HandshakeError(Exception):
    """Session handshake failed."""


class Session:
    """Encrypted session state for one WebSocket connection."""

    def __init__(self, key: bytes, nonce_prefix: bytes):
        self.key = key
        self.nonce_prefix = nonce_prefix  # 4 bytes
        self.tx_counter = 0
        self.rx_counter = -1  # accept counter >= 0
        self._gcm = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> dict:
        """Encrypt plaintext, return envelope dict for JSON serialization."""
        counter = self.tx_counter
        self.tx_counter += 1
        nonce = self.nonce_prefix + struct.pack(">Q", counter)  # 4 + 8 = 12 bytes
        ct = self._gcm.encrypt(nonce, plaintext, None)
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
        nonce = self.nonce_prefix + struct.pack(">Q", counter)
        ct = base64.b64decode(envelope["ct"])
        tag = base64.b64decode(envelope["tag"])
        return self._gcm.decrypt(nonce, ct + tag, None)

    def encrypt_json(self, obj: dict) -> str:
        """Encrypt a JSON-serializable dict, return JSON envelope string."""
        plaintext = json.dumps(obj).encode()
        return json.dumps(self.encrypt(plaintext))

    def decrypt_json(self, envelope_str: str) -> dict:
        """Decrypt a JSON envelope string, return parsed dict."""
        envelope = json.loads(envelope_str)
        plaintext = self.decrypt(envelope)
        return json.loads(plaintext)


def derive_session(psk: bytes, client_rand: bytes, server_rand: bytes) -> Session:
    """Derive session key and nonce prefix from PSK + random challenges."""
    salt = client_rand + server_rand  # 1024 bytes

    session_key = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=salt,
        info=SESSION_INFO,
    ).derive(psk)

    nonce_prefix = HKDF(
        algorithm=SHA256(),
        length=4,
        salt=None,
        info=NONCE_INFO,
    ).derive(session_key)

    return Session(session_key, nonce_prefix)


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
    return derive_session(psk, client_rand, server_rand)


def complete_handshake_server(psk: bytes, hello: dict, server_rand: bytes) -> Session:
    """Server side: complete handshake after receiving hello."""
    if hello.get("type") != "hello":
        raise HandshakeError(f"Expected hello, got {hello.get('type')}")
    if hello.get("version") != PROTOCOL_VERSION:
        raise HandshakeError(f"Protocol version mismatch: {hello.get('version')}")
    client_rand = base64.b64decode(hello["client_rand"])
    if len(client_rand) != RAND_SIZE:
        raise HandshakeError(f"Invalid client_rand size: {len(client_rand)}")
    return derive_session(psk, client_rand, server_rand)


# ── PSK management ────────────────────────────────────────────────────────


def generate_psk() -> bytes:
    """Generate a new 32-byte PSK."""
    return os.urandom(32)


def psk_to_bytes(psk_str: str) -> bytes:
    """SHA-256 hash a passphrase into 32 raw bytes for the AES-256 handshake.

    Must match the key derivation in blaueis-gw configure (psk_to_key).
    """
    import hashlib

    psk_str = psk_str.strip()
    if not psk_str:
        raise ValueError("PSK is empty — configure encryption or use --no-encrypt")
    return hashlib.sha256(psk_str.encode("utf-8")).digest()


def load_psk(config_path: str) -> bytes:
    """Load PSK from a config file. Expects a line: psk = <hex>."""
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    psk_hex = cfg.get("gateway", "psk", fallback=None)
    if not psk_hex:
        raise ValueError(f"No PSK found in {config_path}")
    return bytes.fromhex(psk_hex.strip())
