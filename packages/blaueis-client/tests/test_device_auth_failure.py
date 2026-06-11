"""Reconnect auth-failure handling (session-protocol v2).

An AuthenticationError during reconnect signals a *confirmed* credential
problem (key confirmation failed) — the loop must STOP and fire
``on_auth_failed`` exactly once, instead of retrying a wrong key forever.
Plain HandshakeError (slot pool full, malformed reply) and ordinary
connection errors keep the existing retry-forever behaviour.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from blaueis.client.device import Device
from blaueis.core.crypto import AuthenticationError, HandshakeError


def _device() -> Device:
    dev = Device("localhost", 8765, psk=b"\x00" * 32)
    dev._running = True
    return dev


@pytest.mark.asyncio
async def test_authentication_error_stops_reconnect_and_fires_callback():
    dev = _device()
    failures: list[str] = []
    dev.on_auth_failed = failures.append

    with (
        patch.object(dev, "_connect", AsyncMock(side_effect=AuthenticationError("PSK mismatch"))),
        patch("blaueis.client.device.RECONNECT_DELAYS", [0]),
    ):
        await dev._reconnect()

    assert failures == ["PSK mismatch"]
    assert dev._running is False  # loop stopped, no endless retry


@pytest.mark.asyncio
async def test_plain_handshake_error_keeps_retrying():
    """slot_pool_full and other non-credential handshake failures are
    transient — the loop must NOT stop or fire on_auth_failed."""
    dev = _device()
    failures: list[str] = []
    dev.on_auth_failed = failures.append

    attempts = 0

    async def pool_full_then_ok():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise HandshakeError("Gateway refused connection: slot_pool_full")

    with (
        patch.object(dev, "_connect", pool_full_then_ok),
        patch.object(dev, "_post_connect_init", AsyncMock()),
        patch("blaueis.client.device.RECONNECT_DELAYS", [0, 0, 0]),
    ):
        await dev._reconnect()
        await asyncio.sleep(0)

    assert attempts == 3  # retried through the refusals, then connected
    assert failures == []  # never misclassified as auth failure
    assert dev._running is True


@pytest.mark.asyncio
async def test_ordinary_error_keeps_retrying():
    dev = _device()
    failures: list[str] = []
    dev.on_auth_failed = failures.append

    attempts = 0

    async def flaky_connect():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("unreachable")

    with (
        patch.object(dev, "_connect", flaky_connect),
        patch.object(dev, "_post_connect_init", AsyncMock()),
        patch("blaueis.client.device.RECONNECT_DELAYS", [0, 0, 0]),
    ):
        await dev._reconnect()
        # allow the spawned post-connect task to be scheduled + reaped
        await asyncio.sleep(0)

    assert attempts == 3  # retried through failures, then connected
    assert failures == []  # never misclassified as auth failure
    assert dev._running is True
