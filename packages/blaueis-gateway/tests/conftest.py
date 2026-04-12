"""Test configuration for blaueis-gateway.

Legacy script-style tests are excluded from pytest collection.
Run them standalone:  python packages/blaueis-gateway/tests/test_protocol.py
"""

collect_ignore = [
    "test_configure.py",
    "test_integration.py",
    "test_protocol.py",
    "test_uart_raw.py",
]
