"""The Go node browser engine embeds byte-identical copies of the Python engine resources."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "app/browser/pyworker/resources"
GO = ROOT / "node/browser/resources"
NAMES = {
    "injected.js",
    "bridge.js",
    "keys.json",
    "LICENSE",
    "NOTICE",
    "ThirdPartyNotices.txt",
    "provenance.json",
}


def test_go_engine_resources_are_byte_identical_to_python_engine():
    python = {p.name for p in PYTHON.iterdir() if p.is_file()}
    go = {p.name for p in GO.iterdir() if p.is_file()}
    assert python == go == NAMES
    for name in NAMES:
        assert (PYTHON / name).read_bytes() == (GO / name).read_bytes(), name


def test_go_engine_embeds_every_resource():
    source = (ROOT / "node/browser/resources.go").read_text()
    embed = next(line for line in source.splitlines() if line.startswith("//go:embed"))
    assert set(embed.split()[1:]) == {f"resources/{name}" for name in NAMES}
