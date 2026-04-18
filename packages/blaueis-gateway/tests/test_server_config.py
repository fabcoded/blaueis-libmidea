"""Tests for server.py load_config() and get_pi_stats() — pure functions, no network.

These functions are tested by importing them indirectly to avoid the
uart_protocol import chain (which pulls frame builders not yet in core).

Usage:  python -m pytest packages/blaueis-gateway/tests/test_server_config.py -v
"""

import configparser
import os
import platform
import tempfile
from pathlib import Path


def _load_server_functions():
    """Import load_config and get_pi_stats without triggering full server import.

    server.py does 'from blaueis.gateway.uart_protocol import UartProtocol'
    at module level, and uart_protocol imports frame builders not yet in core.
    We read the source and exec only the functions we need.
    """
    server_path = Path(__file__).resolve().parent.parent / "src" / "blaueis" / "gateway" / "server.py"
    source = server_path.read_text()

    # Create a module-like namespace with the stdlib imports the functions need
    ns = {
        "configparser": configparser,
        "os": os,
        "platform": platform,
        "__name__": "blaueis.gateway.server",
    }
    # Extract just load_config and get_pi_stats function defs + their imports
    import ast

    tree = ast.parse(source)
    func_names = {"load_config", "get_pi_stats"}
    func_sources = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in func_names:
            func_sources.append(ast.get_source_segment(source, node))

    for fsrc in func_sources:
        exec(compile(fsrc, str(server_path), "exec"), ns)

    return ns["load_config"], ns["get_pi_stats"]


load_config, get_pi_stats = _load_server_functions()


# ── load_config: defaults ───────────────────────────────────────────────


def test_defaults_no_paths():
    cfg = load_config()
    assert cfg["psk"] == ""
    assert cfg["uart_port"] == "/dev/serial0"
    assert cfg["uart_baud"] == 9600
    assert cfg["ws_host"] == "0.0.0.0"
    assert cfg["ws_port"] == 8765
    assert cfg["max_queue"] == 16
    assert cfg["frame_spacing_ms"] == 150
    assert cfg["stats_interval"] == 60
    assert cfg["fake_ip"] == "192.168.1.100"
    assert cfg["signal_level"] == 4
    assert cfg["log_level"] == "INFO"
    assert cfg["device_name"] == "Midea AC"


# ── load_config: legacy INI ────────────────────────────────────────────


def test_legacy_ini_full():
    ini_content = """\
[gateway]
psk = aabbccdd
uart_port = /dev/ttyUSB0
uart_baud = 4800
ws_host = 127.0.0.1
ws_port = 9000
max_queue = 16
frame_spacing_ms = 200
stats_interval = 30
fake_ip = 10.0.0.5
signal_level = 2
log_level = DEBUG
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
        f.write(ini_content)
        f.flush()
        cfg = load_config(legacy_path=f.name)

    assert cfg["psk"] == "aabbccdd"
    assert cfg["uart_port"] == "/dev/ttyUSB0"
    assert cfg["uart_baud"] == 4800
    assert cfg["ws_host"] == "127.0.0.1"
    assert cfg["ws_port"] == 9000
    assert cfg["max_queue"] == 16
    assert cfg["frame_spacing_ms"] == 200
    assert cfg["stats_interval"] == 30
    assert cfg["fake_ip"] == "10.0.0.5"
    assert cfg["signal_level"] == 2
    assert cfg["log_level"] == "DEBUG"


def test_legacy_ini_missing_section():
    ini_content = "[other]\nkey = value\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
        f.write(ini_content)
        f.flush()
        cfg = load_config(legacy_path=f.name)

    # Should return defaults when [gateway] section missing
    assert cfg["psk"] == ""
    assert cfg["ws_port"] == 8765


# ── load_config: YAML ──────────────────────────────────────────────────


def test_yaml_instance_only():
    yaml_content = """\
device:
  name: Living Room AC
  serial_port: /dev/ttyAMA0
  baud_rate: 9600
websocket:
  host: 0.0.0.0
  port: 8888
security:
  psk: deadbeef
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load_config(instance_path=f.name)

    assert cfg["device_name"] == "Living Room AC"
    assert cfg["uart_port"] == "/dev/ttyAMA0"
    assert cfg["ws_port"] == 8888
    assert cfg["psk"] == "deadbeef"


def test_yaml_global_plus_instance():
    global_yaml = """\
logging:
  level: WARNING
"""
    instance_yaml = """\
device:
  name: Bedroom AC
  serial_port: /dev/serial0
websocket:
  port: 8765
security:
  psk: cafe
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as gf, \
         tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as inf:
        gf.write(global_yaml)
        gf.flush()
        inf.write(instance_yaml)
        inf.flush()
        cfg = load_config(global_path=gf.name, instance_path=inf.name)

    assert cfg["log_level"] == "WARNING"
    assert cfg["device_name"] == "Bedroom AC"
    assert cfg["psk"] == "cafe"


def test_yaml_missing_keys_fall_back():
    yaml_content = """\
device:
  name: Minimal AC
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load_config(instance_path=f.name)

    assert cfg["device_name"] == "Minimal AC"
    # Everything else should be defaults
    assert cfg["uart_port"] == "/dev/serial0"
    assert cfg["ws_port"] == 8765
    assert cfg["psk"] == ""


def test_yaml_nonexistent_global_ignored():
    """Global path that doesn't exist should be silently skipped."""
    yaml_content = "device:\n  name: Test\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load_config(global_path="/nonexistent/path.yaml", instance_path=f.name)

    assert cfg["device_name"] == "Test"
    assert cfg["log_level"] == "INFO"  # default, not from missing global


# ── get_pi_stats ────────────────────────────────────────────────────────


def test_pi_stats_returns_dict():
    stats = get_pi_stats()
    assert isinstance(stats, dict)


def test_pi_stats_type_field():
    stats = get_pi_stats()
    assert stats["type"] == "pi_status"


def test_pi_stats_has_expected_keys():
    stats = get_pi_stats()
    expected = {"type", "uptime_s", "cpu_percent", "ram_total_mb", "ram_used_mb", "temp_c", "platform"}
    assert expected.issubset(stats.keys())


def test_pi_stats_no_crash_on_any_platform():
    """get_pi_stats should never raise — all /proc reads are wrapped in try/except."""
    stats = get_pi_stats()
    # On Linux (our dev env), we should get real values
    assert isinstance(stats["uptime_s"], (int, float))
    assert isinstance(stats["platform"], str)
