from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pwa_release_validation", ROOT / "scripts/release_validation.py")
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


@pytest.fixture
def release(tmp_path):
    # A real extracted-tree layout; no production build directory is touched.
    root = tmp_path / "release"
    for directory in validator.REQUIRED_DIRS:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for filename in validator.REQUIRED_FILES:
        (root / filename).write_text("test fixture\n")
    (root / "app/__init__.py").write_text('__version__ = "1.2.3"\n')
    (root / "pyproject.toml").write_text('[project]\nname = "openbear"\nversion = "1.2.3"\n')
    (root / "uv.lock").write_text('[[package]]\nname = "openbear"\nversion = "1.2.3"\n')
    (root / "release-meta.json").write_text(json.dumps({"schema": 1, "version": "1.2.3"}))
    dist = root / "web/dist"
    (dist / "assets").mkdir()
    (dist / "assets/index.js").write_text('export const fixture = true;\n')
    (dist / "index.html").write_text('<script type="module" src="/assets/index.js"></script>')
    return root


def add_pwa(release):
    dist = release / "web/dist"
    shutil.copy2(ROOT / "web/public/manifest.webmanifest", dist)
    shutil.copytree(ROOT / "web/public/icons", dist / "icons")
    shutil.copytree(ROOT / "web/public/assets/brand", dist / "assets/brand")
    shutil.copy2(ROOT / "web/src/theme-tokens.css", dist / "assets/theme-tokens.css")
    html = (ROOT / "web/index.html").read_text().replace('/src/main.js', '/assets/index.js')
    html = html.replace('/src/theme-tokens.css', '/assets/theme-tokens.css')
    (dist / "index.html").write_text(html)
    return dist


def change_manifest(dist, change):
    path = dist / "manifest.webmanifest"
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))


def test_historical_release_without_manifest_remains_valid_and_keeps_response_shape(release):
    report = validator.validate_release_tree(release, "1.2.3")
    assert report["ok"]
    assert report["frontend"] == {"indexReferences": 1, "checkedResources": 1}


def test_new_pwa_tree_includes_real_icons_and_preserves_version_checks(release):
    add_pwa(release)
    report = validator.validate_release_tree(release, "v1.2.3")
    assert report["ok"]
    assert report["frontend"] == {"indexReferences": 6, "checkedResources": 8}
    with pytest.raises(validator.ReleaseValidationError, match="版本不一致"):
        validator.validate_release_tree(release, "1.2.4")


@pytest.mark.parametrize("filename", [
    "manifest.webmanifest", "icons/openbear-192.png", "icons/openbear-512.png", "icons/apple-touch-icon.png", "assets/theme-tokens.css",
    *[f"assets/brand/{path.name}" for path in sorted((ROOT / "web/public/assets/brand").glob("favicon-*"))],
])
def test_new_pwa_tree_rejects_missing_files_even_manifest_only_indirect_512(release, filename):
    dist = add_pwa(release)
    (dist / filename).unlink()
    with pytest.raises(validator.ReleaseValidationError):
        validator.validate_release_tree(release, "1.2.3")


@pytest.mark.parametrize("link", ["manifest", "apple-touch-icon"])
def test_new_pwa_tree_rejects_missing_html_links(release, link):
    dist = add_pwa(release)
    index = dist / "index.html"
    index.write_text(index.read_text().replace(f'rel="{link}"', 'rel="other"'))
    with pytest.raises(validator.ReleaseValidationError):
        validator.validate_release_tree(release, "1.2.3")


@pytest.mark.parametrize("key", ["id", "start_url", "scope"])
@pytest.mark.parametrize("value", ["https://other.example/", "//other.example/", "./?session=secret", "./#session", "/chat", "", None])
def test_manifest_cannot_bind_server_or_session(release, key, value):
    dist = add_pwa(release)
    change_manifest(dist, lambda manifest: manifest.__setitem__(key, value))
    with pytest.raises(validator.ReleaseValidationError):
        validator.validate_release_tree(release, "1.2.3")


@pytest.mark.parametrize("value", ["https://other.example/icon.png", "//other.example/icon.png", "../../outside.png", "./icons/missing.png", "./icons/openbear-192.png?token=x"])
def test_manifest_icons_must_resolve_to_public_local_files(release, value):
    dist = add_pwa(release)
    change_manifest(dist, lambda manifest: manifest["icons"][0].__setitem__("src", value))
    with pytest.raises(validator.ReleaseValidationError):
        validator.validate_release_tree(release, "1.2.3")


@pytest.mark.parametrize("mode", ["remove-512", "svg-type", "wrong-size", "invalid-json", "not-object", "bad-name", "bad-display"])
def test_manifest_incomplete_or_invalid_content_is_rejected(release, mode):
    dist = add_pwa(release)
    if mode == "invalid-json":
        (dist / "manifest.webmanifest").write_text("{")
    elif mode == "not-object":
        (dist / "manifest.webmanifest").write_text("[]")
    else:
        def change(manifest):
            if mode == "remove-512":
                manifest["icons"].pop()
            elif mode == "svg-type":
                manifest["icons"][0]["type"] = "image/svg+xml"
            elif mode == "wrong-size":
                manifest["icons"][0]["sizes"] = "512x512"
            elif mode == "bad-name":
                manifest["name"] = ""
            elif mode == "bad-display":
                manifest["display"] = "browser"
        change_manifest(dist, change)
    with pytest.raises(validator.ReleaseValidationError):
        validator.validate_release_tree(release, "1.2.3")


@pytest.mark.parametrize("mode", ["html", "truncated", "wrong-size", "corrupt-pixels"])
def test_release_validates_png_data_not_just_filenames(release, mode):
    dist = add_pwa(release)
    path = dist / "icons/openbear-192.png"
    data = path.read_bytes()
    if mode == "html":
        path.write_bytes(b"<html>not an icon</html>")
    elif mode == "truncated":
        path.write_bytes(data[:33])
    elif mode == "wrong-size":
        shutil.copy2(dist / "icons/apple-touch-icon.png", path)
    else:
        damaged = bytearray(data)
        damaged[-20] ^= 255
        path.write_bytes(damaged)
    with pytest.raises(validator.ReleaseValidationError, match="图标无效"):
        validator.validate_release_tree(release, "1.2.3")


def test_public_symlink_outside_release_is_rejected(release, tmp_path):
    dist = add_pwa(release)
    outside = tmp_path / "outside.png"
    path = dist / "icons/openbear-192.png"
    shutil.copy2(path, outside)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(validator.ReleaseValidationError, match="越界"):
        validator.validate_release_tree(release, "1.2.3")
