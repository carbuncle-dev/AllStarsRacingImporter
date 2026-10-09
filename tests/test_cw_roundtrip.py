"""Round-trip tests against payloads extracted from the user's CrossWorlds install.

Run `python -m cw.blob extracted/cw/legacy extracted/cw/blobs` first; tests skip
when the extracted data is not present (game assets are never committed).
"""

from pathlib import Path

import pytest

from cw.collision import parse_collision, write_collision

BLOBS = Path(__file__).resolve().parent.parent / "extracted" / "cw" / "blobs"
COLLISION = sorted(BLOBS.glob("*_Collision.bin"))


@pytest.mark.skipif(not COLLISION, reason="no extracted CrossWorlds collision payloads")
@pytest.mark.parametrize("path", COLLISION, ids=lambda p: p.stem)
def test_collision_roundtrip(path):
    data = path.read_bytes()
    assert write_collision(parse_collision(data)) == data


from cw.navigation import parse_navigation, write_navigation  # noqa: E402

NAVIGATION = sorted(BLOBS.glob("*_Navigation.bin"))


@pytest.mark.skipif(not NAVIGATION, reason="no extracted CrossWorlds navigation payloads")
@pytest.mark.parametrize("path", NAVIGATION, ids=lambda p: p.stem)
def test_navigation_roundtrip(path):
    data = path.read_bytes()
    assert write_navigation(parse_navigation(data)) == data


from cw.compath import parse_compath, write_compath  # noqa: E402

COMPATH = sorted(BLOBS.glob("*_ComPath*.bin"))


@pytest.mark.skipif(not COMPATH, reason="no extracted CrossWorlds ComPath payloads")
@pytest.mark.parametrize("path", COMPATH, ids=lambda p: p.stem)
def test_compath_roundtrip(path):
    data = path.read_bytes()
    assert write_compath(parse_compath(data)) == data
