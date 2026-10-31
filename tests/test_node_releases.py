"""Node builds from GitHub Releases when this checkout has none of its own."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.instances.servers import LOCAL
from app.servers import install as installer
from app.servers import releases
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

NAME = 'openbear-node-linux-amd64'


FEATURES = list(releases.REQUIRED_FEATURES)


def _build(version, protocol=1, data=None, *, features=FEATURES, protocol_min=None):
    data = data or f'node {version}'.encode()
    manifest = {'schema': 1, 'version': version, 'protocol': protocol, 'toolchain': 'go version go1.25 linux/amd64',
                'binaries': {NAME: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}}}
    if features is not None:
        manifest['features'] = features
    if protocol_min is not None:
        manifest['protocolMin'] = protocol_min
    return manifest, data


@pytest.fixture
async def github(tmp_path, monkeypatch):
    """A stand-in for the repository's tag list, releases by tag and asset downloads."""
    files, rows, hits, overrides, pages = {}, {}, [], {}, []

    def release(tag, manifest, data, **flags):
        prefix = f'/assets/{tag}'
        files[f'{prefix}/manifest.json'] = json.dumps(manifest).encode()
        files[f'{prefix}/{NAME}'] = data
        rows[tag] = {'tag_name': tag, 'draft': flags.get('draft', False), 'prerelease': flags.get('prerelease', False),
                     'assets': [{'name': n, 'browser_download_url': f'{base}{prefix}/{n}'} for n in ('manifest.json', NAME)]}

    def tag(name):
        """A tag with no release behind it (never made, or deleted: withdrawn)."""
        rows[name] = None

    def withdraw(name):
        rows[name] = None

    async def refs(request):
        if 'refs' in overrides:
            return web.json_response(overrides['refs'])
        page, per = int(request.query.get('page', 1)), int(request.query.get('per_page', 30))
        pages.append(page)
        names = [n for n in rows if n.startswith(request.match_info['prefix'])]
        return web.json_response([{'ref': f'refs/tags/{n}'} for n in names[(page - 1) * per:page * per]])

    async def by_tag(request):
        row = rows.get(request.match_info['tag'])
        # GitHub answers 404 for a draft too.
        return web.json_response(row) if row and not row['draft'] else web.json_response({'message': 'Not Found'}, status=404)

    async def asset(request):
        hits.append(request.path)
        if request.path in files and overrides.get('trickle') and request.path.endswith(NAME):
            response = web.StreamResponse()
            await response.prepare(request)
            for byte in files[request.path]:
                await response.write(bytes([byte]))
                await asyncio.sleep(0.2)
            return response
        return web.Response(body=files[request.path]) if request.path in files else web.Response(status=404)

    app = web.Application()
    app.router.add_get('/repos/o/r/git/matching-refs/tags/{prefix}', refs)
    app.router.add_get('/repos/o/r/releases/tags/{tag}', by_tag)
    app.router.add_get('/assets/{tag}/{name}', asset)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url('')).rstrip('/')
    monkeypatch.setattr(releases, 'ALLOW_PLAIN_HTTP', True)
    monkeypatch.setattr(releases, '_api', lambda: f'{base}/repos/o/r')
    monkeypatch.setattr(installer, 'BIN', tmp_path / 'no-local-build')
    releases.configure(tmp_path / 'node-bin')
    try:
        yield type('GitHub', (), {'release': staticmethod(release), 'tag': staticmethod(tag), 'withdraw': staticmethod(withdraw), 'hits': hits,
                                  'files': files, 'overrides': overrides, 'pages': pages})
    finally:
        await server.close()
        releases.configure(None)


def test_versions_compare_numerically_and_development_builds_are_oldest():
    assert releases.newer('1.0.0', '0.13.1') and releases.newer('1.0.10', '1.0.9') and releases.newer('1.0.0', 'dev')
    assert not releases.newer('1.0.0', '1.0.0') and not releases.newer('0.9.0', '1.0.0') and not releases.newer('', '1.0.0')
    # One ordering for every comparison: release > rc > beta > dev of the same X.Y.Z.
    assert releases.newer('1.1.0', '1.1.0-rc.2') and releases.newer('1.1.0-rc.1', '1.1.0-beta.9') and releases.newer('1.1.0-beta.1', '1.1.0-dev.30')


async def test_check_takes_the_newest_release_for_this_protocol(github):
    github.release('v0.13.1', *_build('0.13.1'))  # a controller release: not a node tag
    github.release('node-v1.0.0', *_build('1.0.0'))
    github.release('node-v1.0.1', *_build('1.0.1'))
    github.release('node-v1.1.0', *_build('1.1.0', protocol=2, protocol_min=2))  # needs a newer controller
    github.release('node-v1.2.0', *_build('1.2.0'), draft=True)
    github.release('node-v1.3.0', *_build('1.3.0'), prerelease=True)  # marked pre-release on GitHub
    github.release('node-v1.4.0-rc.1', *_build('1.4.0-rc.1'))  # a test version
    result = await releases.check()
    assert result['ok'] and result['latest'] == '1.0.1' and result['incompatible'] == '1.1.0', result
    assert result['prerelease'] is False and '1.2.0' in result['withdrawn']
    built = installer.manifest()
    assert built['source'] == 'github' and built['version'] == '1.0.1' and NAME in built['binaries']
    # Only the manifest is fetched; binaries wait until a node needs one.
    assert not any(hit.endswith(NAME) for hit in github.hits)
    assert installer.binary(NAME) is None


async def test_a_release_speaking_a_wider_protocol_range_is_taken(github):
    github.release('node-v2.0.0', *_build('2.0.0', protocol=2, protocol_min=1))
    result = await releases.check()
    assert result['latest'] == '2.0.0' and not result['incompatible']


async def test_a_release_without_a_needed_feature_is_passed_over(github):
    github.release('node-v1.0.0', *_build('1.0.0'))
    github.release('node-v1.1.0', *_build('1.1.0', features=[f for f in FEATURES if f != 'net']))
    github.release('node-v1.2.0', *_build('1.2.0', features=None))  # does not say: not taken from GitHub
    result = await releases.check()
    assert result['latest'] == '1.0.0' and result['lacking'] == '1.2.0', result
    assert releases.problem(_build('1.1.0', features=['jobs'])[0]) == 'features'
    assert releases.problem(_build('1.2.0', features=None)[0], local=True) == ''


async def test_test_versions_only_with_prereleases_on(github):
    github.release('node-v1.0.0', *_build('1.0.0'))
    github.release('node-v1.1.0-beta.1', *_build('1.1.0-beta.1'))
    github.release('node-v1.1.0-rc.1', *_build('1.1.0-rc.1'), prerelease=True)
    assert (await releases.check())['latest'] == '1.0.0'
    taking = [True]
    releases.configure(releases._cache, prerelease=lambda: taking[0])
    assert releases.stale()  # the setting changed since the last check
    result = await releases.check()
    assert result['latest'] == '1.1.0-rc.1' and result['prerelease'] is True and installer.manifest()['version'] == '1.1.0-rc.1'
    assert not releases.stale()
    # Turning it off stops using the test version at once, before the next check picks a release.
    taking[0] = False
    assert installer.manifest() == {} and releases.stale()
    assert (await releases.check())['latest'] == '1.0.0' and installer.manifest()['version'] == '1.0.0'


async def test_switching_test_versions_checks_again_at_once_with_one_shared_check(github):
    """Turning 接收测试版 off drops a cached test version; the check that follows the switch
    picks the release right away, and every caller that notices the switch shares it."""
    github.release('node-v1.0.0', *_build('1.0.0'))
    github.release('node-v1.1.0-rc.1', *_build('1.1.0-rc.1'))
    taking = [True]
    releases.configure(releases._cache, prerelease=lambda: taking[0])
    assert not releases.switched() and await releases.follow_switch()  # never checked: not a switch, nothing to do
    assert (await releases.check())['latest'] == '1.1.0-rc.1'
    assert not releases.switched()
    taking[0] = False
    assert releases.switched() and installer.manifest() == {}
    before = len(github.pages)
    done = await asyncio.gather(*(releases.follow_switch() for _ in range(3)))
    assert done == [True, True, True] and len(github.pages) == before + 1  # one check for all three
    assert installer.manifest()['version'] == '1.0.0' and releases.state()['prerelease'] is False
    # And back on: the test version again, without waiting for the daily check.
    taking[0] = True
    assert await releases.follow_switch() and installer.manifest()['version'] == '1.1.0-rc.1'


async def test_a_caller_waits_only_so_long_and_the_check_goes_on(github, monkeypatch):
    github.release('node-v1.0.0', *_build('1.0.0'))
    taking = [False]
    releases.configure(releases._cache, prerelease=lambda: taking[0])
    await releases.check()
    real, gate = releases._tags, asyncio.Event()

    async def slow(http, base):
        await gate.wait()
        return await real(http, base)
    monkeypatch.setattr(releases, '_tags', slow)
    taking[0] = True
    assert await releases.follow_switch(0.05) is False  # still checking: the caller goes on with what it has
    gate.set()
    assert await releases.follow_switch() is True and releases.state()['prerelease'] is True


async def test_the_loop_follows_a_switch_within_seconds(github, monkeypatch):
    github.release('node-v1.0.0', *_build('1.0.0'))
    github.release('node-v1.1.0-beta.1', *_build('1.1.0-beta.1'))
    taking = [False]
    releases.configure(releases._cache, prerelease=lambda: taking[0])
    monkeypatch.setattr(releases, 'SWITCH_POLL', 0.05)
    task = asyncio.create_task(releases.loop())
    try:
        for _ in range(100):
            if releases.state():
                break
            await asyncio.sleep(0.05)
        assert releases.state()['latest'] == '1.0.0'
        taking[0] = True  # well inside the loop's 60 s pause
        for _ in range(100):
            if releases.state().get('latest') == '1.1.0-beta.1':
                break
            await asyncio.sleep(0.05)
        assert releases.state()['latest'] == '1.1.0-beta.1' and installer.manifest()['version'] == '1.1.0-beta.1'
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_the_tag_list_is_paged_through(github):
    """Node releases are found by their tags, never pushed out by a long list of controller releases."""
    for n in range(1, 131):
        github.tag(f'node-v0.0.{n}')  # tags without releases
    github.release('node-v0.1.0', *_build('0.1.0'))  # the 131st tag: on the second page
    result = await releases.check()
    assert result['latest'] == '0.1.0' and github.pages == [1, 2], result


async def test_a_withdrawn_release_is_no_longer_used(github, monkeypatch):
    github.release('node-v1.0.0', *_build('1.0.0'))
    github.release('node-v1.0.1', *_build('1.0.1'))
    assert (await releases.check())['latest'] == '1.0.1'
    github.withdraw('node-v1.0.1')  # the release is deleted; its tag stays
    result = await releases.check()
    assert result['latest'] == '1.0.0' and result['withdrawn'] == ['1.0.1']
    assert installer.manifest()['version'] == '1.0.0' and releases.withdrawn() == {'1.0.1'}
    assert not releases.cache_dir('1.0.1').exists()
    # A check that cannot reach GitHub keeps what the last one found.
    monkeypatch.setattr(releases, '_api', lambda: 'http://127.0.0.1:9/repos/o/r')
    failed = await releases.check()
    assert failed['error'] == 'github_unreachable' and failed['latest'] == '1.0.0' and failed['withdrawn'] == ['1.0.1']


async def test_a_binary_is_downloaded_once_and_checked(github):
    manifest, data = _build('1.0.0')
    github.release('node-v1.0.0', manifest, data)
    await releases.check()
    path = await installer.fetch_binary(NAME)
    assert path and path.read_bytes() == data and installer.binary(NAME) == path
    assert await installer.fetch_binary(NAME) == path
    assert sum(hit.endswith(NAME) for hit in github.hits) == 1
    assert await installer.fetch_binary('openbear-node-darwin-arm64') is None  # not in this release


async def test_a_binary_that_fails_its_checksum_is_not_kept(github):
    manifest, data = _build('1.0.0')
    github.release('node-v1.0.0', manifest, data)
    github.files[f'/assets/node-v1.0.0/{NAME}'] = data[:-1] + b'!'
    await releases.check()
    assert await installer.fetch_binary(NAME) is None
    assert not any(releases.cache_dir('1.0.0').glob(NAME + '*'))


async def test_a_download_that_trickles_is_cut_off(github, monkeypatch):
    manifest, data = _build('1.0.0')
    github.release('node-v1.0.0', manifest, data)
    await releases.check()
    github.overrides['trickle'] = True
    monkeypatch.setattr(releases, 'DOWNLOAD_DEADLINE', 0.5)
    assert await installer.fetch_binary(NAME) is None
    assert not any(releases.cache_dir('1.0.0').glob(NAME + '*'))


async def test_an_unexpected_listing_is_reported_not_raised(github):
    for listing in ({'message': 'API rate limit exceeded'}, ['x', {'ref': 'refs/tags/node-v1.0.0'}, {'ref': 5}]):
        github.overrides['refs'] = listing
        result = await releases.check()
        assert not result['ok'] and result['error'] == 'no_compatible_node_release', result


async def test_the_daily_check_survives_a_failing_check(github, monkeypatch):
    calls, sleeps = [], []

    async def failing():
        calls.append(1)
        raise OSError('disk full')

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(releases, 'check', failing)
    monkeypatch.setattr(releases, 'asyncio', SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await releases.loop()
    assert len(calls) == 2 and sleeps == [3600, 3600]


async def test_a_local_build_wins_over_releases(github, tmp_path, monkeypatch):
    github.release('node-v2.0.0', *_build('2.0.0'))
    await releases.check()
    local = tmp_path / 'local-build'
    local.mkdir()
    manifest, data = _build('1.0.0')
    (local / 'manifest.json').write_text(json.dumps(manifest))
    (local / NAME).write_bytes(data)
    monkeypatch.setattr(installer, 'BIN', local)
    assert installer.manifest()['source'] == 'local' and installer.binary(NAME) == local / NAME
    # A development build without a features list still counts: older build scripts did not write one.
    (local / 'manifest.json').write_text(json.dumps(_build('1.1.0-dev.3', features=None)[0]))
    assert installer.manifest()['version'] == '1.1.0-dev.3' and installer.local_problem() == ''


@pytest.mark.parametrize('local_build, reason', [
    (dict(protocol=2, protocol_min=2), 'protocol'),
    (dict(features=['fs.edit', 'jobs']), 'features'),
])
async def test_a_local_build_this_controller_cannot_use_gives_way_to_the_release(github, tmp_path, monkeypatch, local_build, reason):
    github.release('node-v2.0.0', *_build('2.0.0'))
    await releases.check()
    local = tmp_path / 'local-build'
    local.mkdir()
    (local / 'manifest.json').write_text(json.dumps(_build('9.0.0', **local_build)[0]))
    monkeypatch.setattr(installer, 'BIN', local)
    assert installer.local_problem() == reason
    assert installer.manifest()['source'] == 'github' and installer.manifest()['version'] == '2.0.0'


async def test_only_the_two_newest_releases_stay_cached(github):
    for version in ('1.0.0', '1.0.1', '1.0.2'):
        github.release(f'node-v{version}', *_build(version))
        await releases.check()
    assert sorted(p.name for p in releases.cache_dir('x').parent.iterdir() if p.is_dir()) == ['1.0.1', '1.0.2']


async def test_unreachable_github_is_reported_without_losing_the_cache(github, monkeypatch):
    github.release('node-v1.0.0', *_build('1.0.0'))
    await releases.check()
    monkeypatch.setattr(releases, '_api', lambda: 'http://127.0.0.1:9/repos/o/r')
    result = await releases.check()
    assert not result['ok'] and result['error'] == 'github_unreachable'
    assert installer.manifest()['version'] == '1.0.0'


async def test_servers_page_fetches_the_release_when_nothing_is_built(env, github):  # noqa: F811
    e = env
    manifest, data = _build('1.0.0')
    github.release('node-v1.0.0', manifest, data)
    node = (await api(e, 'GET', '/api/servers'))['node']
    assert node['available'] is False and node['version'] == ''
    checked = (await api(e, 'POST', '/api/servers/node-release/check'))['release']
    assert checked['available'] and checked['version'] == '1.0.0' and checked['source'] == 'github' and checked['checkedAtMs']
    assert (await api(e, 'GET', '/api/servers/node-release'))['release'] == checked
    script = (await e.app.get('/api/server-nodes/v1/install.sh'))
    assert manifest['binaries'][NAME]['sha256'] in await script.text()
    download = await e.app.get(f'/api/server-nodes/v1/download/{NAME}')
    assert download.status == 200 and await download.read() == data
    assert (await e.app.get('/api/server-nodes/v1/download/openbear-node-linux-arm64')).status == 404


async def test_installing_asks_github_first_when_nothing_is_built(env, github):  # noqa: F811
    e = env
    github.release('node-v1.0.0', *_build('1.0.0'))
    sid = (await api(e, 'POST', '/api/servers', {'name': 'fresh'}))['item']['id']
    enrollment = await api(e, 'POST', f'/api/servers/{sid}/enrollment', {})
    assert enrollment['ok'] and installer.manifest()['version'] == '1.0.0'


async def test_no_install_command_without_a_node_build(env, github):  # noqa: F811
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'nothing-yet'}))['item']['id']
    refused = await api(e, 'POST', f'/api/servers/{sid}/enrollment', {}, status=503)
    assert refused['error'] == 'node_binary_missing'


async def test_the_local_server_keeps_the_default_browser(env):  # noqa: F811
    e = env
    refused = await api(e, 'PUT', f'/api/servers/{LOCAL}/browser', {'enabled': True}, status=409)
    assert refused['error'] == 'browser_local_default'
    refused = await api(e, 'POST', f'/api/servers/{LOCAL}/enrollment', {'browser': True}, status=409)
    assert refused['error'] == 'browser_local_default'
