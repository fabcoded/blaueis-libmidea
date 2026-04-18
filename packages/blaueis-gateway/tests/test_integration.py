#!/usr/bin/env python3
"""Integration tests: client ↔ gateway WebSocket communication.

Tests plaintext mode, encrypted handshake, and the HvacClient library
against a mock WebSocket server (no real UART).

Usage:
    python test_integration.py
"""

import asyncio
import json
import sys

import websockets
from blaueis.client.ws_client import HvacClient
from blaueis.core.crypto import (
    complete_handshake_client,
    complete_handshake_server,
    create_hello,
    create_hello_ok,
    generate_psk,
)


async def run_tests():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
        else:
            failed += 1
            print(f"  [FAIL] {name}: {detail}")

    # ── Test 1: Plaintext ping + frame ack ────────────────────────

    async def plaintext_handler(ws):
        async for msg in ws:
            data = json.loads(msg)
            if data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif data.get("type") == "frame":
                await ws.send(json.dumps({"type": "ack", "ref": data.get("ref"), "status": "queued"}))

    async with websockets.serve(plaintext_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            # Ping
            await ws.send(json.dumps({"type": "ping"}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            check("plaintext ping → pong", resp["type"] == "pong")

            # Frame
            await ws.send(json.dumps({"type": "frame", "hex": "AA 0B FF", "ref": 42}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            check("plaintext frame → ack", resp["type"] == "ack")
            check("plaintext ack ref matches", resp["ref"] == 42)

    # ── Test 2: Encrypted handshake + message exchange ────────────

    psk = generate_psk()

    async def encrypted_handler(ws):
        hello_raw = await ws.recv()
        hello = json.loads(hello_raw)
        hello_ok_msg, server_rand = create_hello_ok()
        session = complete_handshake_server(psk, hello, server_rand)
        await ws.send(json.dumps(hello_ok_msg))
        async for raw_msg in ws:
            msg = session.decrypt_json(raw_msg)
            if msg.get("type") == "ping":
                await ws.send(session.encrypt_json({"type": "pong"}))
            elif msg.get("type") == "frame":
                await ws.send(session.encrypt_json({"type": "ack", "ref": msg.get("ref"), "status": "queued"}))

    async with websockets.serve(encrypted_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            hello_msg, client_rand = create_hello()
            await ws.send(json.dumps(hello_msg))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            session = complete_handshake_client(psk, client_rand, reply)

            # Encrypted ping
            await ws.send(session.encrypt_json({"type": "ping"}))
            resp = session.decrypt_json(await asyncio.wait_for(ws.recv(), timeout=2.0))
            check("encrypted ping → pong", resp["type"] == "pong")

            # Encrypted frame
            await ws.send(session.encrypt_json({"type": "frame", "hex": "AA 23 AC", "ref": 7}))
            resp = session.decrypt_json(await asyncio.wait_for(ws.recv(), timeout=2.0))
            check("encrypted frame → ack", resp["type"] == "ack")
            check("encrypted ack ref matches", resp["ref"] == 7)

    # ── Test 3: Wrong PSK rejected ────────────────────────────────

    wrong_psk = generate_psk()

    async def strict_handler(ws):
        hello_raw = await ws.recv()
        hello = json.loads(hello_raw)
        hello_ok_msg, server_rand = create_hello_ok()
        server_session = complete_handshake_server(psk, hello, server_rand)
        await ws.send(json.dumps(hello_ok_msg))
        try:
            raw_msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            server_session.decrypt_json(raw_msg)
            await ws.send(json.dumps({"type": "error", "msg": "should not reach"}))
        except Exception:
            await ws.send(json.dumps({"type": "error", "msg": "decrypt failed"}))

    async with websockets.serve(strict_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            hello_msg, client_rand = create_hello()
            await ws.send(json.dumps(hello_msg))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            bad_session = complete_handshake_client(wrong_psk, client_rand, reply)

            await ws.send(bad_session.encrypt_json({"type": "ping"}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            check("wrong PSK → decrypt fails", resp.get("msg") == "decrypt failed")

    # ── Test 4: HvacClient library (no-encrypt) ──────────────────

    received_types = []

    async def lib_handler(ws):
        async for msg in ws:
            data = json.loads(msg)
            received_types.append(data["type"])
            if data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif data.get("type") == "frame":
                await ws.send(json.dumps({"type": "ack", "ref": data.get("ref"), "status": "queued"}))

    async with websockets.serve(lib_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = HvacClient("127.0.0.1", port, no_encrypt=True)
        await client.connect()

        # Ping
        await client.send_ping()
        resp = json.loads(await client._ws.recv())
        check("HvacClient ping → pong", resp["type"] == "pong")

        # Raw frame (clients build frames themselves; gateway just forwards)
        ref = await client.send_frame("AA 23 AC 8F 00 00 00 00 00 02")
        resp = json.loads(await client._ws.recv())
        check("HvacClient send_frame → ack", resp["type"] == "ack")
        check("HvacClient ref matches", resp["ref"] == ref)

        await client.close()
        check("HvacClient server received ping", "ping" in received_types)
        check("HvacClient server received frame", "frame" in received_types)

    # ── Summary ──────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


def main():
    return asyncio.run(run_tests())


if __name__ == "__main__":
    sys.exit(main())
