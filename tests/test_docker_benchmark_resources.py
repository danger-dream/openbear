import hashlib
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from app.instances import remote
from app.instances.contracts import InstanceError
from app.servers import bench_packages as packages

ROOT = Path(__file__).resolve().parents[1]


def test_docker_context_lists_only_pinned_benchmark_files():
    expected = {p.relative_to(ROOT).as_posix() for p in packages.source_files()}
    lines = (ROOT / '.dockerignore').read_text().splitlines()
    assert lines[2] == '**'
    exceptions = {line[1:] for line in lines if line.startswith('!benchmark-package/')}
    assert exceptions == expected
    assert not any('*' in line for line in exceptions)
    assert 'benchmark-package/service-unlock/v0.1.0/NOTICE.md' in exceptions
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'COPY benchmark-package/ ./benchmark-package/' in dockerfile.split('FROM python:')[1]


def source_tree(tmp_path):
    shutil.copytree(ROOT / 'benchmark-package', tmp_path / 'benchmark-package')
    (tmp_path / 'web/dist').mkdir(parents=True)
    (tmp_path / 'web/dist/index.html').write_text('<!doctype html>')
    shutil.copy2(ROOT / 'Dockerfile', tmp_path / 'Dockerfile')
    return tmp_path


def test_instance_source_archive_includes_verified_packages_not_caches(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    extras = ['benchmark-package/private.json', 'benchmark-package/geekbench5/v0.1.0/bin/geekbench',
              'benchmark-package/geekbench5/v0.1.0/cache/archive.tar.gz', 'benchmark-package/geekbench5/v9.0.0/private.json']
    for name in extras:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('not an image input')
    monkeypatch.setattr(remote, 'ROOT', root)
    expected = {p.relative_to(root).as_posix(): p for p in packages.source_files(root / 'benchmark-package')}
    with tarfile.open(fileobj=io.BytesIO(remote._source_context())) as archive:
        names = set(archive.getnames())
        assert {name for name in names if name.startswith('benchmark-package/')} == set(expected)
        assert not names.intersection(extras)
        for name, source in expected.items():
            assert hashlib.sha256(archive.extractfile(name).read()).digest() == hashlib.sha256(source.read_bytes()).digest()


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'symlink'])
def test_instance_context_refuses_broken_or_linked_runtime_resource(tmp_path, monkeypatch, damage):
    root = source_tree(tmp_path)
    target = root / 'benchmark-package/geekbench5/v0.1.0/run.sh'
    if damage == 'corrupt':
        target.write_text('not the pinned resource')
    else:
        content = target.read_bytes()
        target.unlink()
        if damage == 'symlink':
            outside = tmp_path / 'unrelated.sh'
            outside.write_bytes(content)
            target.symlink_to(outside)
    monkeypatch.setattr(remote, 'ROOT', root)
    with pytest.raises(InstanceError, match='benchmark_package_invalid'):
        remote._source_context()
