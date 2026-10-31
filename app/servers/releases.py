"""Node builds for installs and updates. A local build in node/bin wins (a
development checkout) when it can work with this controller; otherwise the newest
GitHub Release tagged node-v<version> that the user takes (releases, plus beta and
rc with 接收测试版 on), that speaks a protocol this controller speaks and that has
every feature this controller needs. Its manifest is cached in the data directory
and each binary is fetched when first needed, checked by size and sha256 against
that manifest.

Withdrawing a node version: delete its GitHub Release (or turn it back into a draft).
The next check no longer finds it, the controller stops installing it, and nodes
running it are told they must update."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx

from app import versioning
from app.logging import get_logger
from app.servers import compat

log = get_logger("servers.releases")

PROTOCOL = compat.PROTOCOL  # (min, max) of node/protocol.go ProtocolMin..ProtocolVersion this controller speaks
# What this controller calls on nodes beyond protocol 1 (node/protocol.go Features). A
# node build without one of them cannot serve every page and tool, so it is not picked,
# and a node running without one has to update.
REQUIRED_FEATURES = ('fs.edit', 'jobs', 'exec.shell', 'mesh', 'net', 'ddns')
FEATURE_LABELS = {'fs.edit': '文件编辑', 'jobs': '后台任务', 'exec.shell': '指定 shell 执行命令', 'mesh': '组网', 'net': '网络测试', 'ddns': '动态域名'}
CHECK_EVERY = 24 * 3600
NAMES = ('openbear-node-linux-amd64', 'openbear-node-linux-arm64', 'openbear-node-darwin-arm64', 'openbear-node-windows-amd64.exe')
MAX_BINARY = 64 << 20
DOWNLOAD_DEADLINE = 600
USER_AGENT = 'OpenBear-NodeCheck/1.0'
TAG_PREFIX = 'node-v'
# Releases looked at per check, newest first, before giving up; the tag list itself is paged in full.
MAX_CANDIDATES = 20
MAX_TAG_PAGES = 20
_SHA = re.compile(r'[0-9a-f]{64}\Z')
# A failed check says nothing about the releases themselves: these carry over from the last good one.
_KEPT_ON_FAILURE = ('latest', 'incompatible', 'lacking', 'withdrawn')

_cache: Path | None = None
_prerelease = lambda: False  # noqa: E731 - replaced by configure()
_locks: dict[str, asyncio.Lock] = {}
# The check started because 接收测试版 changed; callers that find the switch share it.
_switch_check: asyncio.Task | None = None
# How often the daily loop looks at 接收测试版 between checks (an in-memory setting and a small file).
SWITCH_POLL = 2
# Release assets come over https only; tests serve a local stand-in.
ALLOW_PLAIN_HTTP = False


def _link(url):
    url = str(url or '')
    return url.startswith('https://') or ALLOW_PLAIN_HTTP and url.startswith('http://')


def configure(cache: Path, *, prerelease=None):
    """Where release manifests and binaries are cached; prerelease() says whether the user takes beta and rc."""
    global _cache, _prerelease
    _cache = cache
    _prerelease = prerelease or (lambda: False)


def taking_prereleases() -> bool:
    try:
        return bool(_prerelease())
    except Exception:  # noqa: BLE001 - a broken setting reads as the default
        return False


def version_key(value):
    version = versioning.parse(value)
    return version.key if version else None


def newer(candidate, current):
    """candidate is later than current; see app.versioning (an unknown current is older than any version)."""
    return versioning.newer(candidate, current)


def _read(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def protocol_range(manifest):
    """(min, max) protocol a build speaks; a manifest without protocolMin speaks only its protocol."""
    top = manifest.get('protocol') if isinstance(manifest, dict) else None
    low = manifest.get('protocolMin', top) if isinstance(manifest, dict) else None
    if type(top) is not int or type(low) is not int or not 1 <= low <= top:
        return None
    return low, top


def features_of(manifest):
    """The features a build lists, or None when its manifest does not say."""
    value = manifest.get('features') if isinstance(manifest, dict) else None
    return frozenset(str(f) for f in value) if isinstance(value, list) else None


def missing_features(features):
    """Required features a node or build lacks; nothing when it is unknown which it has."""
    return [] if features is None else [f for f in REQUIRED_FEATURES if f not in features]


def problem(manifest, *, local=False):
    """Why a well-formed build cannot be used by this controller: 'protocol', 'features', or ''.
    A local build whose manifest does not list features is taken (older build scripts did
    not write them; a node still says what it has when it connects); a release must list them."""
    span = protocol_range(manifest)
    if span is None or compat.common(PROTOCOL, span) is None:
        return 'protocol'
    features = features_of(manifest)
    if features is None:
        return '' if local else 'features'
    return 'features' if missing_features(features) else ''


def valid_manifest(value, version=None, *, local=False):
    """A well-formed manifest: a version (development ones only from a local build), a protocol and checked binaries."""
    if not isinstance(value, dict):
        return False
    binaries = value.get('binaries')
    stated = value.get('version')
    return bool(value.get('schema') == 1 and protocol_range(value)
                and (versioning.valid(stated) if local else versioning.publishable(stated))
                and (version is None or stated == version) and isinstance(binaries, dict) and binaries
                and all(name in NAMES and isinstance(b, dict) and _SHA.fullmatch(str(b.get('sha256', '')))
                        and type(b.get('bytes')) is int and 0 < b['bytes'] <= MAX_BINARY for name, b in binaries.items()))


def cache_dir(version):
    return _cache / version


def state():
    return _read(_cache / 'state.json') if _cache else {}


def cached_manifest(prerelease=None):
    """The release the last check picked, when its manifest is cached and the user still
    takes that kind of version; binaries may still be missing. A version that later checks
    no longer find (withdrawn) is never used, whatever is left in the cache."""
    if _cache is None:
        return {}
    version = str(state().get('latest') or '')
    if not versioning.offered(version, taking_prereleases() if prerelease is None else prerelease):
        return {}
    value = _read(cache_dir(version) / 'manifest.json')
    if not valid_manifest(value, version) or problem(value):
        return {}
    return {**value, 'source': 'github'}


def withdrawn():
    return frozenset(str(v) for v in state().get('withdrawn') or [] if isinstance(v, str))


def _client(timeout):
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json'}
    if token := (os.environ.get('OPENBEAR_GITHUB_TOKEN') or '').strip():
        headers['Authorization'] = f'Bearer {token}'
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=True, headers=headers)


def _api():
    from app.update.service import GITHUB_API
    return GITHUB_API


async def _tags(http, base):
    """Every node-v<version> tag in the repository, paged through: a busy controller
    release history never pushes node releases out of view."""
    versions, seen = set(), set()
    for page in range(1, MAX_TAG_PAGES + 1):
        response = await http.get(f'{base}/git/matching-refs/tags/{TAG_PREFIX}', params={'per_page': 100, 'page': page})
        if response.status_code != 200:
            raise RuntimeError(f'github_http_{response.status_code}')
        rows = response.json()
        rows = rows if isinstance(rows, list) else []
        refs = {str(row.get('ref', '')) for row in rows if isinstance(row, dict)}
        # matching-refs may ignore paging and return every match each time.
        if not refs - seen:
            break
        seen |= refs
        for ref in refs:
            name = ref.removeprefix('refs/tags/')
            if name.startswith(TAG_PREFIX) and versioning.publishable(name[len(TAG_PREFIX):]):
                versions.add(name[len(TAG_PREFIX):])
        if len(rows) < 100:
            break
    return versions


def stale(prerelease=None):
    """A check is due: a day has passed, or the user changed whether beta and rc are taken."""
    last = state()
    wanted = taking_prereleases() if prerelease is None else prerelease
    return time.time() - last.get('checkedAtMs', 0) / 1000 >= CHECK_EVERY or bool(last.get('prerelease', False)) != wanted


def switched(prerelease=None):
    """The last check was made for the other 接收测试版 setting, so its pick may be one the
    user no longer takes (a beta after turning it off) or miss one they now take. Never
    having checked is not a switch: the daily check covers that."""
    if _cache is None:
        return False
    last = state()
    wanted = taking_prereleases() if prerelease is None else prerelease
    return bool(last) and bool(last.get('prerelease', False)) != wanted


def _switch_done(task):
    if not task.cancelled() and task.exception() is not None:
        log.warning('节点版本检查失败', 原因=str(task.exception())[:120])


async def follow_switch(wait=None):
    """Check again right away when 接收测试版 changed since the last check, instead of leaving
    the old pick (or none) until the next daily check. Every caller shares one check; wait
    bounds how long this caller waits for it (None: until it is done), the check itself
    goes on. Returns whether the pick now follows the setting."""
    global _switch_check
    if not switched():
        return True
    if _switch_check is None or _switch_check.done():
        _switch_check = asyncio.create_task(check(only_if_switched=True), name='node-release-switch')
        _switch_check.add_done_callback(_switch_done)
    await asyncio.wait({_switch_check}, timeout=wait)
    return not switched()


async def check(*, api=None, prerelease=None, only_if_switched=False):
    """Find the newest node release this controller can use and cache its manifest.
    A newer release for a protocol this controller does not speak is reported, not used: it
    needs a newer controller; one without a feature this controller needs is reported as lacking."""
    if _cache is None:
        return {'ok': False, 'error': 'node_release_cache_unset'}
    prerelease = taking_prereleases() if prerelease is None else bool(prerelease)
    async with _locks.setdefault('check', asyncio.Lock()):
        if only_if_switched and not switched(prerelease):
            return {'ok': True, **state()}  # a check that ran meanwhile already follows the setting
        previous = state()
        result = {'checkedAtMs': int(time.time() * 1000), 'prerelease': prerelease, 'latest': '', 'incompatible': '', 'lacking': '',
                  'withdrawn': [], 'error': ''}
        base = api or _api()
        try:
            async with _client(30) as http:
                offered = sorted((v for v in await _tags(http, base) if versioning.offered(v, prerelease)), key=version_key, reverse=True)
                for version in offered[:MAX_CANDIDATES]:
                    found = await _usable(http, base, version, prerelease, result)
                    if found:
                        manifest, assets = found
                        result['latest'] = version
                        target = cache_dir(version)
                        if not (target / 'manifest.json').is_file():
                            _write(target / 'assets.json', {n: assets[n] for n in manifest['binaries']})
                            _write(target / 'manifest.json', manifest)
                            log.info('节点新版本已就绪', 版本=version)
                        break
                else:
                    result['error'] = 'no_compatible_node_release'
        except (httpx.HTTPError, ValueError):
            result['error'] = 'github_unreachable'
        except RuntimeError as exc:
            result['error'] = str(exc)[:120]
        if result['error'] not in ('', 'no_compatible_node_release'):
            for key in _KEPT_ON_FAILURE:
                result[key] = previous.get(key, result[key])
        _write(_cache / 'state.json', result)
        _prune(result)
        return {'ok': not result['error'], **result}


async def _usable(http, base, version, prerelease, result):
    """(manifest, asset links) of one release, or None after noting why it cannot be used."""
    response = await http.get(f'{base}/releases/tags/{TAG_PREFIX}{version}')
    if response.status_code == 404:
        # The tag is there but its release is gone (or still a draft): withdrawn.
        result['withdrawn'].append(version)
        return None
    if response.status_code != 200:
        raise RuntimeError(f'github_http_{response.status_code}')
    row = response.json()
    if not isinstance(row, dict) or row.get('draft'):
        result['withdrawn'].append(version)
        return None
    if row.get('prerelease') and not prerelease:
        # Marked pre-release on GitHub: only for those who take test versions.
        return None
    assets = row.get('assets') if isinstance(row.get('assets'), list) else []
    links = {a.get('name'): a.get('browser_download_url') for a in assets if isinstance(a, dict)}
    if not _link(links.get('manifest.json')):
        return None
    manifest = await http.get(links['manifest.json'])
    value = manifest.json() if manifest.status_code == 200 else {}
    if not valid_manifest(value, version):
        return None
    reason = problem(value)
    if reason == 'protocol':
        if protocol_range(value)[0] > PROTOCOL[1]:
            result['incompatible'] = result['incompatible'] or version
        return None
    if reason == 'features':
        result['lacking'] = result['lacking'] or version
        return None
    if not all(_link(links.get(n)) for n in value['binaries']):
        return None
    return value, links


def _prune(result):
    """Keep the release in use and the newest other one; withdrawn releases go at once."""
    gone = set(result.get('withdrawn') or [])
    versions = sorted((version_key(e.name), e) for e in _cache.iterdir() if e.is_dir() and version_key(e.name))
    others = [e.name for _, e in versions if e.name not in gone and e.name != result.get('latest')]
    keep = {result.get('latest'), *others[-1:]} if result.get('latest') else set(others[-2:])
    for _, entry in versions:
        if entry.name in keep:
            continue
        for f in entry.iterdir():
            f.unlink(missing_ok=True)
        entry.rmdir()


async def fetch(name):
    """The picked release's binary, downloaded on first use; None when no release lists it."""
    value = cached_manifest()
    binary = (value.get('binaries') or {}).get(name)
    if not binary:
        return None
    target = cache_dir(value['version']) / name
    async with _locks.setdefault(name, asyncio.Lock()):
        if target.is_file() and target.stat().st_size == binary['bytes']:
            return target
        url = _read(cache_dir(value['version']) / 'assets.json').get(name, '')
        if not _link(url):
            return None
        partial = target.with_name(target.name + '.partial')
        digest, size = hashlib.sha256(), 0
        try:
            # httpx timeouts bound each read, not a server that trickles bytes.
            async with asyncio.timeout(DOWNLOAD_DEADLINE), _client(60) as http, http.stream('GET', url) as response:
                if response.status_code != 200:
                    raise RuntimeError(f'github_http_{response.status_code}')
                with partial.open('wb') as out:
                    async for chunk in response.aiter_bytes(1 << 20):
                        size += len(chunk)
                        if size > binary['bytes']:
                            raise RuntimeError('node_download_too_large')
                        digest.update(chunk)
                        out.write(chunk)
            if size != binary['bytes'] or digest.hexdigest() != binary['sha256']:
                raise RuntimeError('node_download_checksum_mismatch')
            partial.chmod(0o755)
            partial.replace(target)
            log.info('节点程序已下载', 文件=name, 版本=value['version'])
            return target
        except (httpx.HTTPError, RuntimeError, OSError) as exc:  # OSError includes the deadline's TimeoutError
            log.warning('节点程序下载失败', 文件=name, 原因=str(exc)[:120] or type(exc).__name__)
            return None
        finally:
            partial.unlink(missing_ok=True)


async def loop(enabled=lambda: True):
    """Check once a day, and within SWITCH_POLL seconds of 接收测试版 changing; a restart does
    not check again before the day is over. Nothing is checked while enabled() is false (主机 off)."""
    while True:
        if not enabled():
            await asyncio.sleep(300)
            continue
        if switched():
            if not await follow_switch():
                await asyncio.sleep(3600)  # the check itself broke (see the log); retry like a failed daily one
            continue
        if stale():
            try:
                await check()
            except Exception as exc:  # noqa: BLE001 - one bad answer or full disk must not end the daily check
                log.warning('节点版本检查失败', 原因=str(exc)[:120])
                await asyncio.sleep(3600)
                continue
        pause = min(300, max(60, CHECK_EVERY - (time.time() - state().get('checkedAtMs', 0) / 1000)))
        until = time.monotonic() + pause
        while time.monotonic() < until and not (enabled() and switched()):
            await asyncio.sleep(SWITCH_POLL)
