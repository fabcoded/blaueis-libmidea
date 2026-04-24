"""Live integration tests for Device class against real gateway.

Requires gateway at 192.168.210.30:8765. Skipped in CI.

Usage:  python -m pytest packages/blaueis-client/tests/test_device_live.py -v -s

Note on pytest-socket: ``pytest-homeassistant-custom-component`` is
installed in this dev env to support ha-midea integration tests, and
pytest auto-loads its plugin, which calls ``pytest_socket.disable_socket()``
on every test. That's correct for an HA integration test, but blocks
this file. We disable both plugins via the package's ``pyproject.toml``
``addopts``: ``-p no:homeassistant -p no:socket``. libmidea is not an
HA test suite, so dropping them at the runner level is the right
boundary.
"""

import asyncio
import os

import pytest
from blaueis.client.device import Device

GATEWAY_HOST = os.environ.get("BLAUEIS_GW_HOST", "192.168.210.30")
GATEWAY_PORT = int(os.environ.get("BLAUEIS_GW_PORT", "8765"))
# PSK has no default — pass via env var to keep credentials out of source.
# Tests skip cleanly when the env var isn't set (handshake fails fast).
GATEWAY_PSK = os.environ.get("BLAUEIS_GW_PSK", "")

pytestmark = pytest.mark.asyncio


async def _can_reach_gateway() -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(GATEWAY_HOST, GATEWAY_PORT), timeout=3
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


@pytest.fixture(scope="module")
def gateway_available():
    if not GATEWAY_PSK:
        pytest.skip(
            "BLAUEIS_GW_PSK env var not set — live tests skipped. "
            "Export it from your local credentials store (e.g. "
            "``source gateway.local`` then ``export BLAUEIS_GW_PSK=$PSK``) "
            "to enable."
        )
    reachable = asyncio.run(_can_reach_gateway())
    if not reachable:
        pytest.skip(f"Gateway not reachable at {GATEWAY_HOST}:{GATEWAY_PORT}")


async def test_connect_and_discover(gateway_available):
    """Connect, query B5, verify capabilities discovered."""
    device = Device(GATEWAY_HOST, GATEWAY_PORT, psk=GATEWAY_PSK)
    try:
        await device.start()

        assert device.connected
        assert device.capabilities_received

        avail = device.available_fields
        assert len(avail) > 50, f"Expected >50 available fields, got {len(avail)}"

        for field in ["power", "operating_mode", "target_temperature",
                      "fan_speed", "indoor_temperature"]:
            assert field in avail, f"Missing core field: {field}"

        print(f"\nAvailable fields: {len(avail)}")
    finally:
        await device.stop()


async def test_poll_and_read(gateway_available):
    """Connect, poll, verify database-driven queries work."""
    device = Device(GATEWAY_HOST, GATEWAY_PORT, psk=GATEWAY_PSK, poll_interval=999)
    try:
        await device.start()

        # Queries computed from database — should include C0 at minimum
        queries = device.required_queries
        assert "cmd_0x41" in queries

        # Manually trigger a poll
        await device._send_poll_queries()
        await asyncio.sleep(2)

        # Read from the database
        power = device.read("power")
        target = device.read("target_temperature")
        indoor = device.read("indoor_temperature")

        print(f"\nPower: {power}, Target: {target}, Indoor: {indoor}")

        assert isinstance(power, bool)
        assert target is not None
        assert 12 <= target <= 43

    finally:
        await device.stop()


async def test_state_change_fires(gateway_available):
    """Connect, verify state change callbacks fire on first poll."""
    device = Device(GATEWAY_HOST, GATEWAY_PORT, psk=GATEWAY_PSK, poll_interval=999)
    changes = []

    try:
        # Set callback BEFORE start so we catch changes from initial poll
        device.on_state_change = lambda f, new, old: changes.append((f, new, old))
        await device.start()

        await device._send_poll_queries()
        await asyncio.sleep(2)

        changed_fields = {c[0] for c in changes}
        assert "power" in changed_fields, f"Expected power change, got: {changes}"
        print(f"\nState changes: {len(changes)} fields")

    finally:
        await device.stop()


async def test_required_queries_from_database(gateway_available):
    """Verify queries are derived from B5-confirmed fields in database."""
    device = Device(GATEWAY_HOST, GATEWAY_PORT, psk=GATEWAY_PSK, poll_interval=999)
    try:
        await device.start()

        queries = device.required_queries
        assert "cmd_0x41" in queries
        # After B5, C1 group fields are confirmed → group queries needed
        assert "cmd_0xc1_group1" in queries, f"Expected group1 query, got: {queries}"
        print(f"\nQueries from database: {sorted(queries)}")

    finally:
        await device.stop()
