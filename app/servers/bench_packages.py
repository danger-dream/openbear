"""Benchmark packages: our pinned copies of the tools in benchmark-package/, and the binaries
their manifests pin by SHA256. A binary is downloaded once into the cache, checked, unpacked
there, and from then on served from it."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import aiohttp

ROOT = Path(__file__).resolve().parents[2] / 'benchmark-package'
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600, sock_connect=20, sock_read=60)
MAX_BINARY = 256 << 20
_LOCKS = {}  # (cache, sha256) -> lock, so one download and one unpack serve concurrent runs


class PackageError(Exception):
    def __init__(self, code, detail=''):
        super().__init__(code)
        self.code, self.detail = code, detail


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def manifest(tool, version, root=ROOT):
    path = root / tool / version / 'manifest.json'
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PackageError('package_missing', f'{tool} {version}') from e
    if data.get('tool') != tool or data.get('version') != version:
        raise PackageError('package_invalid', f'{tool} {version}')
    return data


def source_files(root=ROOT):
    """Pinned package inputs for an image, without downloaded binaries or local extras."""
    from app.servers.bench_catalog import PACKAGES
    root = Path(root)
    out = []

    def include(path, digest=None):
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents):
            raise PackageError('package_missing', str(path.relative_to(root)))
        if digest is not None and sha256_file(path) != digest:
            raise PackageError('checksum_mismatch', str(path.relative_to(root)))
        out.append(path)

    include(root / 'README.md')
    for tool, version in PACKAGES.items():
        data = manifest(tool, version, root=root)
        base = root / tool / version
        include(root / tool / 'recipe.json')
        include(base / 'manifest.json')
        for rel, digest in data['files'].items():
            include(base / _safe(rel), digest)
        for name in ('LICENSE', 'NOTICE.md', 'openbear.patch'):
            if (base / name).exists():
                include(base / name)
    return list(dict.fromkeys(out))


def _safe(rel):
    path = PurePosixPath(rel)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise PackageError('package_invalid', rel)
    return str(path)


def _lock(cache, key):
    return _LOCKS.setdefault((str(cache), key), asyncio.Lock())


async def fetch(binary, cache, session=None):
    """The pinned download, from the cache or the network; a wrong checksum is never kept."""
    want = binary['sha256']
    path = cache / want
    async with _lock(cache, want):
        if path.is_file() and await asyncio.to_thread(sha256_file, path) == want:
            return path
        cache.mkdir(parents=True, exist_ok=True)
        own = session is None
        session = session or aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT, trust_env=False)
        fd, temp = tempfile.mkstemp(dir=cache, prefix='.download-')
        try:
            digest, size = hashlib.sha256(), 0
            async with session.get(binary['url'], timeout=DOWNLOAD_TIMEOUT) as response:
                if response.status != 200:
                    raise PackageError('download_failed', f'{binary["url"]}: HTTP {response.status}')
                with os.fdopen(fd, 'wb') as f:
                    fd = None
                    async for block in response.content.iter_chunked(1 << 16):
                        size += len(block)
                        if size > MAX_BINARY:
                            raise PackageError('download_failed', f'{binary["url"]}: too large')
                        digest.update(block)
                        f.write(block)
            if digest.hexdigest() != want:
                raise PackageError('checksum_mismatch', binary['url'])
            os.replace(temp, path)
            return path
        except (aiohttp.ClientError, TimeoutError) as e:
            raise PackageError('download_failed', f'{binary["url"]}: {str(e) or "timed out"}') from e
        finally:
            if fd is not None:
                os.close(fd)
            if os.path.exists(temp):
                os.remove(temp)
            if own:
                await session.close()


def _unpacked_valid(archive, target):
    if not (target / '.done').is_file():
        return False
    try:
        expected = set()
        with tarfile.open(archive) as tar:
            for member in tar:
                if not member.isfile():
                    continue
                rel = _safe(member.name)
                expected.add(rel)
                path = target / rel
                if path.is_symlink() or not path.is_file() or path.stat().st_size != member.size:
                    return False
                digest = hashlib.sha256()
                with tar.extractfile(member) as source:
                    for block in iter(lambda: source.read(1 << 20), b''):
                        digest.update(block)
                if sha256_file(path) != digest.hexdigest():
                    return False
        actual = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file() and not p.is_symlink() and p != target / '.done'}
        return actual == expected
    except (OSError, tarfile.TarError):
        return False


def _unpack(binary, archive, cache):
    """Where the binary's files are once unpacked: [(path under dest, local file, mode)]."""
    if not binary.get('unpack'):
        return [('', archive, 0o755)]
    if binary['unpack'] != 'tar':
        raise PackageError('package_invalid', binary['unpack'])
    target = cache / 'unpacked' / binary['sha256']
    done = target / '.done'
    if not _unpacked_valid(archive, target):
        temp = Path(tempfile.mkdtemp(dir=cache, prefix='.unpack-'))
        try:
            with tarfile.open(archive) as tar:
                tar.extractall(temp, filter='data')
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp, target)
            done.write_text('')
        finally:
            shutil.rmtree(temp, ignore_errors=True)
    if binary.get('member'):
        member = target / _safe(binary['member'])
        if not member.is_file():
            raise PackageError('package_invalid', binary['member'])
        return [('', member, 0o755)]
    strip = int(binary.get('strip') or 0)
    found = []
    for path in sorted(target.rglob('*')):
        rel = path.relative_to(target).parts
        if path.name == '.done' and len(rel) == 1 or not path.is_file() or path.is_symlink() or len(rel) <= strip:
            continue
        found.append(('/'.join(rel[strip:]), path, path.stat().st_mode & 0o777 or 0o644))
    return found


async def files(tool, version, platform, cache, session=None, root=ROOT):
    """Every file the package puts on a node of that platform: [(relative path, local file, mode)]."""
    data = manifest(tool, version, root)
    if platform not in data.get('platforms', []):
        raise PackageError('platform_unsupported', f'{tool} {platform}')
    base = root / tool / version
    out = []
    for rel, digest in data.get('files', {}).items():
        path = base / _safe(rel)
        if not path.is_file() or sha256_file(path) != digest:
            raise PackageError('package_invalid', rel)
        out.append((rel, path, 0o755 if rel.endswith('.sh') else 0o644))
    for binary in data.get('binaries', []):
        if binary.get('platform') != platform:
            continue
        archive = await fetch(binary, cache, session)
        dest = _safe(binary['dest'])
        async with _lock(cache, binary['sha256']):
            unpacked = await asyncio.to_thread(_unpack, binary, archive, cache)
        for sub, path, mode in unpacked:
            out.append((f'{dest}/{sub}' if sub else dest, path, mode))
    return out
