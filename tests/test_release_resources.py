import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.release_support import candidate
from tests.test_release_build_stages import build
from tests.test_pre_commit import _git, _hook, repo  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_tracked_tree_has_no_unapproved_public_source():
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    rejected = [path for path in paths if path and not candidate.valid_path(path)]
    assert rejected == []


@pytest.mark.parametrize('path', [
    'benchmark-package/hardware.check.place/v0.1.0/bin/geekbench5',
    'benchmark-package/geekbench5/v0.1.0/archive.tar.gz',
    'benchmark-package/unknown/v0.1.0/run.sh',
    'benchmark-package/ip.check.place/v0.1.0/main/ref/private.json',
    'node/mesh/testdata/private.json', 'gateway/private.js',
])
def test_resource_allowlist_does_not_allow_neighboring_inputs(path):
    assert not candidate.valid_path(path)


def test_packages_are_pinned_and_included_in_install_tree(tmp_path):
    destination = tmp_path / 'package'
    destination.mkdir()
    build.copy_benchmarks(ROOT, destination)
    files = build.audit_package(destination)
    for tool in candidate.BENCHMARK_FILES:
        manifest = f'benchmark-package/{tool}/v0.1.0/manifest.json'
        assert manifest in files
        base = destination / Path(manifest).parent
        data = json.loads((destination / manifest).read_text())
        for rel, want in data['files'].items():
            assert hashlib.sha256((base / rel).read_bytes()).hexdigest() == want
        for binary in data['binaries']:
            assert {'url', 'sha256', 'dest', 'platform'} <= binary.keys()
    assert not any('/bin/' in name for name in files)


def test_package_copy_rejects_unpinned_resource_and_corruption(tmp_path):
    source = tmp_path / 'source'
    shutil.copytree(ROOT / 'benchmark-package', source / 'benchmark-package')
    extra = source / 'benchmark-package/ip.check.place/v0.1.0/private.txt'
    extra.write_text('not a runtime resource')
    with pytest.raises(build.ReleaseError, match='unexpected benchmark resource'):
        build.copy_benchmarks(source, tmp_path / 'package')
    extra.unlink()
    (source / 'benchmark-package/ip.check.place/v0.1.0/ip.sh').write_text('corrupt')
    with pytest.raises(build.ReleaseError, match='checksum mismatch'):
        build.copy_benchmarks(source, tmp_path / 'package')


def test_public_probe_resource_is_exempt_only_at_its_pinned_path_and_bytes(repo):
    name = 'benchmark-package/ip.check.place/v0.1.0/main/ref/cookies.txt'
    target = repo / name
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / name).read_bytes())
    _git(repo, 'add', name)
    assert _hook(repo).returncode == 0
    target.write_bytes(target.read_bytes() + b'\nchanged\n')
    _git(repo, 'add', name)
    assert _hook(repo).returncode == 1
    target.write_bytes((ROOT / name).read_bytes())
    _git(repo, 'add', name)
    (repo / 'other.txt').write_bytes(target.read_bytes())
    _git(repo, 'add', 'other.txt')
    assert _hook(repo).returncode == 1


def test_pinned_whitespace_policy_still_checks_first_party(repo):
    shutil.copy2(ROOT / '.gitattributes', repo / '.gitattributes')
    path = repo / 'node/browser/resources/LICENSE'
    path.parent.mkdir(parents=True)
    path.write_bytes((ROOT / 'node/browser/resources/LICENSE').read_bytes())
    _git(repo, 'add', '.gitattributes', 'node/browser/resources/LICENSE')
    assert _git(repo, 'diff', '--cached', '--check', check=False).returncode == 0
    (repo / 'first-party.py').write_text('value = 1 \n')
    _git(repo, 'add', 'first-party.py')
    assert _git(repo, 'diff', '--cached', '--check', check=False).returncode != 0
