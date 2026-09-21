"""Tests for the periodic recap: journal lines are broadcast, never echoed back."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest
from blaueis.gateway import server as server_mod
from blaueis.gateway.server import ClientConnection, GatewayServer

REAL_LINE = "2026-09-21T10:00:00+0000 pi blaueis-gw-atelier[1]: connected"
RECAP_LINE = "2026-09-21T10:01:00+0000 pi blaueis-gw-atelier[1]: recap state=RUNNING clients=1"
ECHO_LINE = "2026-09-21T10:01:00+0000 pi blaueis-gw-atelier[1]:   | " + RECAP_LINE
NESTED_ECHO_LINE = "2026-09-21T10:02:00+0000 pi blaueis-gw-atelier[1]:   | " + ECHO_LINE


@pytest.fixture
def server() -> GatewayServer:
    config = {
        "frame_spacing_ms": 0,
        "stats_interval": 0,
        "fake_ip": "10.0.0.1",
        "uart_baud": 9600,
        "slot_pool_size": 4,
        "_instance_path": "/etc/blaueis-gw/instances/atelier.yaml",
    }
    return GatewayServer(config, no_encrypt=True)


class FakeWS:
    remote_address = ("fake", 0)


class FakeClient(ClientConnection):
    """Real ClientConnection, but its `send` just appends to a list."""

    def __init__(self, sid: int):
        super().__init__(FakeWS(), session=None, no_encrypt=True, sid=sid)
        self.sent: list[dict] = []

    async def send(self, msg: dict) -> None:  # type: ignore[override]
        self.sent.append(msg)


# ── _read_journal ─────────────────────────────────────────────────────────


async def test_read_journal_drops_echo_lines(server, monkeypatch) -> None:
    stdout = "\n".join([REAL_LINE, ECHO_LINE, RECAP_LINE, NESTED_ECHO_LINE]) + "\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=stdout, returncode=0))

    assert await server._read_journal(n=10) == [REAL_LINE, RECAP_LINE]


# ── _recap_once ───────────────────────────────────────────────────────────


async def test_recap_logs_only_the_recap_line_at_info(server, monkeypatch, caplog) -> None:
    async def fake_read_journal(n: int = 20) -> list[str]:
        return [REAL_LINE, ECHO_LINE, RECAP_LINE, NESTED_ECHO_LINE]

    monkeypatch.setattr(server, "_read_journal", fake_read_journal)
    server._clients.add(FakeClient(sid=0))

    with caplog.at_level(logging.INFO, logger=server_mod.log.name):
        await server._recap_once()

    info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert len(info) == 1
    assert info[0].startswith("recap state=")
    assert not any("  | " in m for m in info)


async def test_recap_broadcasts_only_non_echo_lines(server, monkeypatch) -> None:
    # Patch journalctl itself so the echo filter in `_read_journal` is in the path.
    stdout = "\n".join([REAL_LINE, ECHO_LINE, RECAP_LINE, NESTED_ECHO_LINE]) + "\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=stdout, returncode=0))
    client = FakeClient(sid=0)
    server._clients.add(client)

    await server._recap_once()

    (msg,) = client.sent
    assert msg["type"] == "journal"
    assert msg["lines"] == [REAL_LINE, RECAP_LINE]
    assert {"state", "clients", "tx_queue", "tx_queue_max", "last_frame_age"} <= msg.keys()


async def test_recap_without_clients_still_logs_the_recap(server, monkeypatch, caplog) -> None:
    async def fake_read_journal(n: int = 20) -> list[str]:
        return [REAL_LINE]

    monkeypatch.setattr(server, "_read_journal", fake_read_journal)

    with caplog.at_level(logging.INFO, logger=server_mod.log.name):
        await server._recap_once()

    info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert len(info) == 1
    assert info[0].startswith("recap state=")
