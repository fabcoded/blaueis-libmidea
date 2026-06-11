"""B5 integrity guard: a capability frame that fails wire integrity (CRC-8 /
checksum) must be discarded, not decoded.

Regression for the fan_speed feature-gate desync — a corrupt B5 whose cap 0x10
byte reads as disabled_0/2 must not flip the universal fan_speed control to
`excluded` (which made it silently unsettable until the integration reloaded).
Clean frames decode normally, so every legitimate cap-gate still applies.
"""

from blaueis.core.codec import load_glossary
from blaueis.core.process import process_b5
from blaueis.core.status import build_status

# A real B5 page-1 body captured from the dev unit (wire envelope stripped).
# Carries cap 0x10 = 0x01 (stepless fan control) among other caps.
REAL_B5 = bytes.fromhex("b508120201011402010115020101160201001a02010110020101250207203c203c203c00240201010100")


def test_untrusted_b5_is_discarded():
    g = load_glossary()
    st = build_status(device="t", glossary=g)
    before = st["fields"]["fan_speed"]["feature_available"]
    process_b5(st, REAL_B5, g, frame_trusted=False)
    fs = st["fields"]["fan_speed"]
    assert fs["feature_available"] == before  # not disabled
    assert "values" not in (fs.get("active_constraints") or {})  # cap not applied
    assert st["meta"]["frame_counts"].get("rsp_0xb5_bad") == 1


def test_trusted_b5_applies_caps():
    g = load_glossary()
    st = build_status(device="t", glossary=g)
    process_b5(st, REAL_B5, g, frame_trusted=True)
    fs = st["fields"]["fan_speed"]
    assert fs["feature_available"] != "excluded"
    assert "values" in (fs.get("active_constraints") or {})  # stepless applied
    assert st["meta"]["frame_counts"].get("rsp_0xb5_bad", 0) == 0


if __name__ == "__main__":
    test_untrusted_b5_is_discarded()
    test_trusted_b5_applies_caps()
    print("ok")
