"""Live integration tests for Device class against real gateway.

Requires gateway at 192.168.210.30:8765. Skipped in CI.

Usage:  python -m pytest packages/blaueis-client/tests/test_device_live.py -v -s
"""

import asyncio
import os

import pytest

from blaueis.client.device import Device

# Skip if no gateway reachable (CI, offline, etc.)
GATEWAY_HOST = os.environ.get("BLAUEIS_GW_HOST", "192.168.210.30")
GATEWAY_PORT = int(os.environ.get("BLAUEIS_GW_PORT", "8765"))
GATEWAY_PSK = os.environ.get(
    "BLAUEIS_GW_PSK", "YG23aC3EWkdmabs2Pc5eWL7vR77fUtY2mzyiwJqglVsB"
)

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
    reachable = asyncio.get_event_loop().run_until_complete(_can_reach_gateway())
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

        # Core fields must be present
        for field in ["power", "operating_mode", "target_temperature",
                      "fan_speed", "indoor_temperature"]:
            assert field in avail, f"Missing core field: {field}"

        print(f"\nAvailable fields: {len(avail)}")
    finally:
        await device.stop()


async def test_poll_and_read(gateway_available):
    """Connect, poll, read status fields."""
    device = Device(GATEWAY_HOST, GATEWAY_PORT, psk=GATEWAY_PSK, poll_interval=999)
    try:
        await device.start()

        # Register key fields and manually trigger a poll
        device.register_fields([
            "power", "operating_mode", "target_temperature",
            "indoor_temperature", "fan_speed",
        ])
        await device._send_poll_queries()
        await asyncio.sleep(2)  # wait for response

        # Read values
        values = device.read_all_registered()
        print(f"\nStatus: {values}")

        # Power should be a bool
        assert isinstance(values["power"], bool)
        # Target temp should be reasonable
        target = values["target_temperature"]
        assert target is not None
        assert 12 <= target <= 43, f"target_temperature={target} out of range"
        # Indoor temp should be reasonable (if available)
        indoor = values["indoor_temperature"]
        if indoor is not None:
            assert -10 < indoor < 60, f"indoor_temperature={indoor} out of range"

    finally:
        await device.stop()


async def test_state_change_fires(gateway_available):
    """Connect, register, verify state change callbacks fire on first poll."""
    device = Device(GATEWAY_HOST, GATEWAY_PORT, psk=GATEWAY_PSK, poll_interval=999)
    changes = []

    try:
        await device.start()

        device.register_fields(["power", "target_temperature"])
        device.on_state_change = lambda f, new, old: changes.append((f, new, old))

        await device._send_poll_queries()
        await asyncio.sleep(2)

        # First poll transitions from None → actual values
        changed_fields = {c[0] for c in changes}
        assert "power" in changed_fields, f"Expected power change, got: {changes}"
        print(f"\nState changes: {changes}")

    finally:
        await device.stop()


async def test_required_queries_dedup(gateway_available):
    """Verify query deduplication works with real glossary."""
    device = Device(GATEWAY_HOST, GATEWAY_PORT, psk=GATEWAY_PSK, poll_interval=999)
    try:
        await device.start()

        # All C0 fields → should only need cmd_0x41
        device.register_fields(["power", "operating_mode", "target_temperature"])
        queries = device.required_queries
        assert "cmd_0x41" in queries
        print(f"\nQueries for C0 fields: {queries}")

    finally:
        await device.stop()
