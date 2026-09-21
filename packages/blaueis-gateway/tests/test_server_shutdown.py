"""Orderly gateway shutdown: no pending tasks, no warnings, no tracebacks.

Seen live on ``systemctl stop``: the signal handler stopped the event
loop under ``run_until_complete`` and closed it, so the websockets
server's close coroutine was never awaited, the keepalive and connection
handler tasks were destroyed while pending, and the handler's ``async
for`` failed with "Event loop is closed".
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
import textwrap

import pytest
import websockets
from blaueis.client.ws_client import HvacClient
from blaueis.core.crypto import psk_to_bytes
from blaueis.gateway.server import GatewayServer, serve_until_signal

PASSPHRASE = "test passphrase"


def _config(**extra) -> dict:
    return {
        "frame_spacing_ms": 0,
        "stats_interval": 60,  # stats loop runs and must be cancelled too
        "fake_ip": "10.0.0.1",
        "uart_baud": 9600,
        "slot_pool_size": 4,
        "ws_host": "127.0.0.1",
        "ws_port": 0,
        "psk": PASSPHRASE,
        **extra,
    }


async def _idle_uart_loop() -> None:
    await asyncio.Event().wait()


@pytest.fixture
def server(monkeypatch) -> GatewayServer:
    srv = GatewayServer(_config(), no_encrypt=False)
    monkeypatch.setattr(srv, "_uart_loop", _idle_uart_loop)
    return srv


async def _connect_client(server: GatewayServer) -> HvacClient:
    while server._ws_server is None:
        await asyncio.sleep(0.01)
    port = server._ws_server.sockets[0].getsockname()[1]
    client = HvacClient("127.0.0.1", port, psk=psk_to_bytes(PASSPHRASE))
    await client.connect()
    while not server._clients:
        await asyncio.sleep(0.01)
    return client


async def _settle() -> None:
    """Let done-callbacks of just-finished tasks run."""
    for _ in range(3):
        await asyncio.sleep(0)


async def test_stop_with_connected_client_leaves_no_pending_tasks(server, caplog) -> None:
    baseline = asyncio.all_tasks()
    with caplog.at_level(logging.INFO):
        run = asyncio.create_task(server.run())
        client = await _connect_client(server)
        listening = asyncio.create_task(client.listen())

        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        await asyncio.wait_for(listening, timeout=5)
        ws = client._ws
        await client.close()
        await _settle()

    assert asyncio.all_tasks() - baseline == set()
    assert ws.close_code == 1001
    assert not server._clients
    assert server.slot_pool.in_use_count == 0
    assert server._ws_server is None
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert noisy == []
    assert any(r.getMessage().startswith("Gateway stopped (1 client") for r in caplog.records)


async def test_sigterm_stops_serve_until_signal_cleanly(server, caplog) -> None:
    baseline = asyncio.all_tasks()
    with caplog.at_level(logging.INFO):
        serving = asyncio.create_task(serve_until_signal(server))
        client = await _connect_client(server)
        listening = asyncio.create_task(client.listen())

        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(serving, timeout=5)  # returns, does not raise
        await asyncio.wait_for(listening, timeout=5)
        await client.close()
        await _settle()

    assert asyncio.all_tasks() - baseline == set()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # handlers removed again: a later SIGTERM would not reach a dead task
    assert asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM) is False


async def test_external_cancel_still_propagates(server) -> None:
    """Only the signal path swallows the cancellation."""
    serving = asyncio.create_task(serve_until_signal(server))
    while server._ws_server is None:
        await asyncio.sleep(0.01)
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving


async def test_client_leaving_during_handshake_is_not_a_traceback(server, caplog) -> None:
    with caplog.at_level(logging.INFO):
        run = asyncio.create_task(server.run())
        while server._ws_server is None:
            await asyncio.sleep(0.01)
        port = server._ws_server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}"):
            pass  # open, then close without sending the crypto hello
        await asyncio.sleep(0.1)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("disconnected during handshake" in r.getMessage() for r in caplog.records)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_real_process_sigterm_leaves_a_clean_journal(tmp_path) -> None:
    """The systemd path end to end: python -m blaueis.gateway.server,
    one connected client, SIGTERM. Exit 0 and nothing on stderr that
    the journal would show as a warning or traceback."""
    port = _free_port()
    instance = tmp_path / "test.yaml"
    instance.write_text(
        textwrap.dedent(
            f"""\
            device:
              serial_port: {tmp_path / "no-such-tty"}
            websocket:
              host: 127.0.0.1
              port: {port}
            security:
              psk: {PASSPHRASE}
            """
        )
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "blaueis.gateway.server",
        "--instance",
        str(instance),
        stderr=subprocess.PIPE,
    )
    try:
        client = HvacClient("127.0.0.1", port, psk=psk_to_bytes(PASSPHRASE))
        for _ in range(100):
            try:
                await client.connect()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            pytest.fail("gateway did not come up")
        listening = asyncio.create_task(client.listen())
        await asyncio.sleep(0.2)

        proc.send_signal(signal.SIGTERM)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        await asyncio.wait_for(listening, timeout=5)
        close_code = client._ws.close_code
        await client.close()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    journal = stderr.decode()
    assert proc.returncode == 0, journal
    assert close_code == 1001
    for bad in ("Traceback", "RuntimeWarning", "Task was destroyed", "Event loop is closed", "never awaited"):
        assert bad not in journal, journal
    assert "Gateway stopped (1 client(s) closed)" in journal
