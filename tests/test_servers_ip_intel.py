"""IP 情报: what each public source's answer is taken for, a server's report from all of them with what is kept
and a source failing on its own, the http:BL key kept in the vault through the settings, Radar read with a
动态域名 Cloudflare account's token, and the open ports probed from the controller."""
from __future__ import annotations

import asyncio
import json
import unittest.mock

from app.servers import ip_intel
from app.webhooks.repository import one
from tests.test_mesh_service import server
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

RDAP_RIPE = {'name': 'SISTEC-AS', 'events': [{'eventAction': 'registration', 'eventDate': '2012-05-18T08:06:09Z'}],
             'entities': [{'roles': ['administrative'], 'handle': 'JCDL', 'vcardArray': ['vcard', [['fn', {}, 'text', 'Juan Carlos']]]},
                          {'roles': ['registrant'], 'handle': 'MNT-SCARTAMA', 'vcardArray': ['vcard', [['fn', {}, 'text', 'MNT-SCARTAMA']]]},
                          {'roles': ['registrant'], 'handle': 'ORG-STS43-RIPE', 'vcardArray': ['vcard', [['fn', {}, 'text', 'SISTEC TELECOM S.L.']]]},
                          {'roles': ['registrant'], 'handle': 'RIPE-NCC-END-MNT', 'vcardArray': ['vcard', [['fn', {}, 'text', 'RIPE-NCC-END-MNT']]]}]}


def test_what_each_source_is_taken_for():
    assert [ip_intel.bogon(ip) for ip in ('10.1.2.3', '100.64.0.1', '192.0.2.5', '127.0.0.1', '8.8.8.8', '45.32.100.1')] == [True] * 4 + [False] * 2
    assert ip_intel.rdap_view(RDAP_RIPE) == {'name': 'SISTEC-AS', 'org': 'SISTEC TELECOM S.L.', 'registered': '2012-05-18'}
    arin = {'name': 'CLOUDFLARENET', 'events': [{'eventAction': 'registration', 'eventDate': '2010-07-14T00:00:00-04:00'}],
            'entities': [{'roles': ['registrant'], 'handle': 'CLOUD14', 'vcardArray': ['vcard', [['fn', {}, 'text', 'Cloudflare, Inc.']]]}]}
    assert ip_intel.rdap_view(arin) == {'name': 'CLOUDFLARENET', 'org': 'Cloudflare, Inc.', 'registered': '2010-07-14'}
    # No registrant (APNIC), the epoch kept for unknown.
    assert ip_intel.rdap_view({'name': 'OVH', 'events': [{'eventAction': 'registration', 'eventDate': '1970-01-01T00:00:00Z'}]}) == \
        {'name': 'OVH', 'org': '', 'registered': ''}
    assert ip_intel.peeringdb_view({'data': [{'name': 'SISTEC TELECOM', 'info_type': 'Cable/DSL/ISP', 'info_traffic': '', 'info_scope': 'Regional'}]}) == \
        {'name': 'SISTEC TELECOM', 'type': 'Cable/DSL/ISP', 'traffic': '', 'scope': 'Regional'}
    assert ip_intel.peeringdb_view({'data': []}) is None
    assert ip_intel.radar_view({'result': {'summary_0': {'bot': '63.411625', 'human': '36.588375'}}}) == {'human': 36.6, 'bot': 63.4}
    assert ip_intel.radar_view({'result': {}}) is None
    assert ip_intel.httpbl_view('127.3.42.5') == {'listed': True, 'days': 3, 'threat': 42, 'kinds': ['suspicious', 'spammer']}
    # A search engine, nothing found.
    assert ip_intel.httpbl_view('127.0.29.0') == ip_intel.httpbl_view('') == {'listed': False}


class Reply:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise OSError(f'HTTP {self.status}')

    async def json(self, content_type=None):
        return self.data


class Web:
    """The sources as the answers say, by a part of the URL; every request and its headers kept."""
    def __init__(self, answers, down=()):
        self.answers, self.down, self.calls = answers, down, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if any(part in url for part in self.down):
            raise OSError('unreachable')
        for part, answer in self.answers.items():
            if part in url:
                return Reply(200, answer)
        return Reply(404, {})


WEB = {'network-info/data.json?resource=45.32.100.1': {'data': {'asns': ['20473'], 'prefix': '45.32.96.0/20'}},
       'routing-status/data.json?resource=AS20473': {'data': {'announced_space': {'v4': {'prefixes': 6, 'ips': 4352}}}},
       'rdap.org/autnum/20473': RDAP_RIPE,
       'peeringdb.com/api/net?asn=20473': {'data': [{'name': 'Vultr', 'info_type': 'Cable/DSL/ISP', 'info_traffic': '1-5Tbps', 'info_scope': 'Global'}]},
       'radar/http/summary/bot_class?asn=20473': {'result': {'summary_0': {'bot': '32.7', 'human': '67.3'}}},
       'network-info/data.json?resource=45.33.0.9': {'data': {'asns': ['64500']}},
       'routing-status/data.json?resource=AS64500': {'data': {'announced_space': {'v4': {'prefixes': 1, 'ips': 256}}}},
       'peeringdb.com/api/net?asn=64500': {'data': []}}


async def test_a_servers_ip_intel_kept_each_source_on_its_own(env):
    e = env
    sid = await server(e, 'Vultr', ipv4='45.32.100.1', online=False)
    web = Web(WEB)
    names = []

    async def ptr(ip):
        return 'vultr.example.com' if ip == '45.32.100.1' else ''

    async def a(name):
        names.append(name)
        return '127.3.42.1'
    nothing = unittest.mock.AsyncMock(return_value=None)
    with unittest.mock.patch.object(e.servers, 'http', web), unittest.mock.patch.object(ip_intel, 'lookup_ptr', ptr), \
            unittest.mock.patch.object(ip_intel, 'lookup_a', a), unittest.mock.patch.object(e.servers.ddns, 'cloudflare_account', nothing):
        # Neither the key nor a Cloudflare account: those two say so; the rest is read.
        view = await api(e, 'GET', f'/api/servers/{sid}/ip-intel')
        assert (view['ip'], view['bogon'], view['rdns'], view['asn']) == ('45.32.100.1', False, 'vultr.example.com', 20473)
        assert view['httpbl'] == view['network']['radar'] == {'state': 'unconfigured'}
        assert view['network']['space'] == {'prefixes': 6, 'ips': 4352}
        assert view['network']['rdap'] == {'name': 'SISTEC-AS', 'org': 'SISTEC TELECOM S.L.', 'registered': '2012-05-18'}
        assert view['network']['peeringdb'] == {'name': 'Vultr', 'type': 'Cable/DSL/ISP', 'traffic': '1-5Tbps', 'scope': 'Global'}
        assert view['ports'] is None and not names
        # The key, through the settings, into the vault; only twelve lowercase letters.
        assert (await api(e, 'PUT', '/api/servers/settings', {'ipIntel': {'httpblKey': 'NOT A KEY'}}, status=400))['error'] == 'invalid_httpbl_key'
        saved = await api(e, 'PUT', '/api/servers/settings', {'ipIntel': {'httpblKey': 'abcdefghijkl'}})
        assert saved['settings']['ipIntel'] == {'httpbl': True, 'cloudflare': ''}
        row = await one(e.db.conn, "SELECT kv_json FROM memory_secrets WHERE name='servers/httpbl'")
        assert json.loads(row['kv_json']) == [{'key': 'key', 'value': 'abcdefghijkl'}]
        assert 'abcdefghijkl' not in (await one(e.db.conn, 'SELECT settings_json FROM server_settings WHERE id=1'))['settings_json']
    account = unittest.mock.AsyncMock(return_value={'alias': 'CF', 'token': 'cf-token'})
    with unittest.mock.patch.object(e.servers, 'http', web), unittest.mock.patch.object(ip_intel, 'lookup_ptr', ptr), \
            unittest.mock.patch.object(ip_intel, 'lookup_a', a), unittest.mock.patch.object(e.servers.ddns, 'cloudflare_account', account):
        view = await api(e, 'GET', f'/api/servers/{sid}/ip-intel')
        assert names == ['abcdefghijkl.1.100.32.45.dnsbl.httpbl.org']
        assert view['httpbl'] == {'state': 'ok', 'listed': True, 'days': 3, 'threat': 42, 'kinds': ['suspicious']}
        assert view['network']['radar'] == {'state': 'ok', 'human': 67.3, 'bot': 32.7}
        assert [h for url, h in web.calls if 'radar' in url] == [{'Authorization': 'Bearer cf-token'}]
        assert (await api(e, 'GET', '/api/servers/settings'))['settings']['ipIntel'] == {'httpbl': True, 'cloudflare': 'CF'}
        # Kept: nothing asked again.
        asked = len(web.calls)
        assert await api(e, 'GET', f'/api/servers/{sid}/ip-intel') == view and len(web.calls) == asked and len(names) == 1
        # RDAP out of reach for another network: only it is missing, and it is not asked again at once.
        other = await server(e, 'Other', ipv4='45.33.0.9', online=False)
        down = Web(WEB, down=('rdap.org',))
        with unittest.mock.patch.object(e.servers, 'http', down):
            view = await api(e, 'GET', f'/api/servers/{other}/ip-intel')
            assert view['network']['rdap'] is None and view['network']['space'] == {'prefixes': 1, 'ips': 256}
            assert view['network']['peeringdb'] == {} and view['network']['radar'] == {'state': 'none'}
            asked = len(down.calls)
            await api(e, 'GET', f'/api/servers/{other}/ip-intel')
            assert len(down.calls) == asked
        # No public IPv4: nothing to read.
        bare = await server(e, 'Bare', online=False)
        assert await api(e, 'GET', f'/api/servers/{bare}/ip-intel') == {'ok': True, 'ip': '', 'ports': None}


async def test_open_ports_probed_from_the_controller(env):
    e = env
    listening = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
    open_port = listening.sockets[0].getsockname()[1]
    probe = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
    closed_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    sid = await server(e, 'Here', ipv4='127.0.0.1', online=False)
    try:
        with unittest.mock.patch.object(ip_intel, 'PORTS', ((open_port, 'Open'), (closed_port, 'Closed'))):
            result = (await api(e, 'POST', f'/api/servers/{sid}/ip-intel/ports'))['ports']
    finally:
        listening.close()
        await listening.wait_closed()
    assert (result['ip'], result['checked'], result['open'], result['fromSelf']) == ('127.0.0.1', 2, [{'port': open_port, 'name': 'Open'}], False)
    # Kept with the server's report: a bogon is not looked up, its ports still shown.
    view = await api(e, 'GET', f'/api/servers/{sid}/ip-intel')
    assert (view['bogon'], view['ports']) == (True, result)
    bare = await server(e, 'Bare', online=False)
    assert (await api(e, 'POST', f'/api/servers/{bare}/ip-intel/ports', status=409))['error'] == 'no_public_ipv4'


async def test_one_read_of_a_source_for_everyone_asking_at_once(env):
    """Two pages of one server, or two servers of one network, opened together: each source is read once."""
    e = env
    a = await server(e, 'A', ipv4='45.32.100.1', online=False)
    b = await server(e, 'B', ipv4='45.32.100.1', online=False)
    web, gate = Web(WEB), asyncio.Event()
    get = web.get

    class Slow:
        def __init__(self, reply):
            self.reply = reply

        async def __aenter__(self):
            await gate.wait()
            return await self.reply.__aenter__()

        async def __aexit__(self, *exc):
            return False
    web.get = lambda url, headers=None, timeout=None: Slow(get(url, headers, timeout))

    async def ptr(ip):
        return ''
    nothing = unittest.mock.AsyncMock(return_value=None)
    with unittest.mock.patch.object(e.servers, 'http', web), unittest.mock.patch.object(ip_intel, 'lookup_ptr', ptr), \
            unittest.mock.patch.object(e.servers.ddns, 'cloudflare_account', nothing):
        pages = [asyncio.create_task(api(e, 'GET', f'/api/servers/{sid}/ip-intel')) for sid in (a, a, b)]
        await asyncio.sleep(0.1)
        gate.set()
        views = await asyncio.gather(*pages)
    assert views[0] == views[1] == views[2] and views[0]['asn'] == 20473
    urls = [url for url, _ in web.calls]
    assert len(urls) == len(set(urls)) == 4, urls


async def test_ports_probed_before_the_address_changed_are_not_shown_for_the_new_one(env):
    e = env
    sid = await server(e, 'Moved', ipv4='127.0.0.1', online=False)
    with unittest.mock.patch.object(ip_intel, 'PORTS', ()):
        probed = (await api(e, 'POST', f'/api/servers/{sid}/ip-intel/ports'))['ports']
    assert (await api(e, 'GET', f'/api/servers/{sid}/ip-intel'))['ports'] == probed
    await e.servers._merge_node_info(sid, {'info': {'platform': 'linux', 'ipv4': '127.0.0.2'}})
    assert (await api(e, 'GET', f'/api/servers/{sid}/ip-intel'))['ports'] is None
