#!/usr/bin/env python3
"""Minimal UART test — compare sync pyserial vs async serial_asyncio.

Run on the Pi to diagnose why the gateway gets no response.

Usage:
    python3 test_uart_raw.py [/dev/serial0]
"""

import asyncio
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/serial0"
BAUD = 9600

# The SN query frame (0x07, broadcast)
SN_QUERY = bytes.fromhex("AA0CFFF30000000000070000FB")


def test_sync():
    """Test with plain synchronous pyserial."""
    print("=== SYNC TEST (pyserial) ===")
    ser = serial.Serial(PORT, BAUD, timeout=2.0)
    ser.reset_input_buffer()

    print(f"  TX: {SN_QUERY.hex(' ')}")
    ser.write(SN_QUERY)
    ser.flush()

    time.sleep(0.2)
    data = ser.read(100)
    if data:
        print(f"  RX ({len(data)}B): {data.hex(' ')}")
        if data[0] == 0xAA:
            print("  -> Got valid frame start (0xAA)")
    else:
        print("  RX: nothing (timeout)")

    ser.close()


async def test_async():
    """Test with async serial_asyncio."""
    print("\n=== ASYNC TEST (serial_asyncio) ===")
    try:
        import serial_asyncio
    except ImportError:
        print("  serial_asyncio not installed! pip3 install pyserial-asyncio")
        return

    reader, writer = await serial_asyncio.open_serial_connection(url=PORT, baudrate=BAUD)

    print(f"  TX: {SN_QUERY.hex(' ')}")
    writer.write(SN_QUERY)
    await writer.drain()

    # Try reading with timeout
    try:
        data = await asyncio.wait_for(reader.read(100), timeout=2.0)
        if data:
            print(f"  RX ({len(data)}B): {data.hex(' ')}")
            if 0xAA in data:
                print("  -> Contains 0xAA frame start")
        else:
            print("  RX: empty")
    except TimeoutError:
        print("  RX: timeout (no data in 2s)")

    writer.close()


async def test_async_byte_by_byte():
    """Test async reading byte by byte (like our gateway does)."""
    print("\n=== ASYNC BYTE-BY-BYTE TEST ===")
    try:
        import serial_asyncio
    except ImportError:
        return

    reader, writer = await serial_asyncio.open_serial_connection(url=PORT, baudrate=BAUD)

    print(f"  TX: {SN_QUERY.hex(' ')}")
    writer.write(SN_QUERY)
    await writer.drain()

    # Read byte by byte like _read_one_frame does
    print("  Reading byte by byte...")
    collected = bytearray()
    try:
        for i in range(100):
            byte = await asyncio.wait_for(reader.read(1), timeout=0.5)
            if not byte:
                print(f"  -> EOF after {i} bytes")
                break
            collected.append(byte[0])
            if i < 5 or byte[0] == 0xAA:
                print(f"  -> byte[{i}] = 0x{byte[0]:02X}")
    except TimeoutError:
        pass

    if collected:
        print(f"  Total: {len(collected)}B: {collected.hex(' ')}")
    else:
        print("  Total: 0 bytes")

    writer.close()


def main():
    print(f"Port: {PORT}, Baud: {BAUD}\n")

    # Sync test first
    test_sync()

    # Async tests
    asyncio.run(test_async())
    asyncio.run(test_async_byte_by_byte())


if __name__ == "__main__":
    main()
