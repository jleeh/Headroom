from headroom import profiles, scan


def test_microcode_revision_parse():
    raw = bytes([0, 0, 0, 0, 0x2B, 0x01, 0, 0])
    assert scan.microcode_revision(raw) == 0x12B
    assert scan.microcode_revision(b"\x00" * 8) is None
    assert scan.microcode_revision(b"\x01") is None


def test_raptor_advisory():
    assert profiles.cpu_advisory({"family": "amd-zen5"}) is None
    assert profiles.cpu_advisory({"family": "intel-raptorlake", "microcode": 0x129})["level"] == "bad"
    assert profiles.cpu_advisory({"family": "intel-raptorlake", "microcode": 0x12F})["level"] == "good"
    assert profiles.cpu_advisory({"family": "intel-raptorlake"})["level"] == "warn"


def test_name_matches_variants():
    assert profiles.name_matches("AMD Ryzen 7 9800X3D", "AMD Ryzen 7 9800X3D 8-Core Processor")
    assert not profiles.name_matches("NVIDIA GeForce RTX 5070", "NVIDIA GeForce RTX 5070 Ti")
    assert not profiles.name_matches("Ryzen 7 9800X", "AMD Ryzen 7 9800X3D")


def test_family_detection():
    assert scan.cpu_family("13th Gen Intel(R) Core(TM) i9-13900K") == "intel-raptorlake"
    assert scan.cpu_family("AMD Ryzen 7 9800X3D 8-Core Processor") == "amd-zen5-x3d"
