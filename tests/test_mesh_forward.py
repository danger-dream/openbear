"""Port forwards in the mesh document: the draft's forwards and settings.forward, what validation
refuses, the access rules and fast ports they open, each node's part of the config (entries, targets,
province sets), removal of a member, status cleaning, the province data from ip2region's xdb files
and the controller fetching it while a forward uses it."""
from __future__ import annotations

import copy
import struct
from types import SimpleNamespace

import pytest

from app.servers import mesh_fast, mesh_forward, mesh_geo, mesh_model as model, mesh_status
from app.servers.mesh import Mesh
from app.servers.mesh_config import build, node_rules
from tests.test_mesh_config import doc, errors, facts, member


def ffacts(**kw):
    f = facts(**kw)
    for sid in f:
        f[sid]['features'] = frozenset({'mesh', mesh_fast.FAST_FEATURE, mesh_forward.FEATURE})
    return f


def fwd(**kw):
    return {'id': 'fw1', 'entry': 'm1', 'listen': 443, 'proto': 'any', 'target': 'r2', 'host': '127.0.0.1', 'port': 8443, 'line': 'mesh',
            'proxyProtocol': 0, 'allow': {'prefixes': [], 'provinces': []}, 'enabled': True, 'note': '', **kw}


def fdoc(*forwards):
    d = doc(member('r1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0}), member('r2', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0}),
            member('m1', 'member', entry={'mode': 'failover', 'autoReturn': True, 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'tcp'}]}),
            member('m2', 'member', entry={'mode': 'failover', 'autoReturn': True, 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}]}))
    d['forwards'] = list(forwards) or [fwd()]
    mesh_fast.ensure_secrets(d)
    return d


def codes(d, f=None, applying=False):
    return {code for _, code in errors(d, f or ffacts(), applying)}


@pytest.fixture
def geo(tmp_path, monkeypatch):
    store = mesh_geo.Store(tmp_path)
    store.sets = {'广东省': ['1.0.8.0/21', '2408:8000::/20'], '北京市': ['1.2.0.0/16']}
    monkeypatch.setattr(mesh_geo, 'STORE', store)
    return store


def test_defaults_and_cleaning():
    d = model.empty_doc()
    assert d['settings']['forward'] == {'port': 51082, 'fastPort': 51083} and d['forwards'] == []
    old = fdoc()
    old['settings'].pop('forward')
    old.pop('forwards')
    model.upgrade(old)
    assert old['settings']['forward'] == {'port': 51082, 'fastPort': 51083} and old['forwards'] == []

    cleaned = model.clean_doc({'forwards': [{'entry': 'm1', 'target': 'r2', 'listen': 443, 'port': 443},
                                            {'entry': 'm2', 'target': 'r2', 'listen': 53, 'port': 53, 'proto': 'udp', 'host': 'DNS.Example.com',
                                             'line': 'fast', 'allow': {'prefixes': ['1.2.3.4', '10.0.0.1/8', '2001:db8::1/32'], 'provinces': ['北京市', '广东省']}}],
                               'settings': {'forward': {'port': 51090}}}, fdoc())
    a, b = cleaned['forwards']
    assert (a['id'], a['proto'], a['host'], a['line'], a['enabled'], a['proxyProtocol']) == ('fw1', 'any', '127.0.0.1', 'mesh', True, 0)
    assert (b['id'], b['host']) == ('fw2', 'dns.example.com')
    assert b['allow'] == {'prefixes': ['1.2.3.4/32', '10.0.0.0/8', '2001:db8::/32'], 'provinces': ['北京市', '广东省']}
    assert cleaned['settings']['forward'] == {'port': 51090, 'fastPort': 51083}
    # Fast forwards need the fast relay's key.
    assert cleaned['settings']['fast']['keys']

    with pytest.raises(model.MeshInvalid) as e:
        model.clean_doc({'forwards': [{'entry': 'm1', 'target': 'r2', 'listen': 0, 'port': 443, 'proto': 'udp', 'proxyProtocol': 1, 'host': 'a b',
                                       'line': 'slow', 'allow': {'prefixes': ['300.1.1.1'], 'provinces': ['火星']}}]}, fdoc())
    assert {x['error'] for x in e.value.errors} >= {'invalid_port', 'invalid_forward_proxy_protocol', 'invalid_forward_host', 'invalid_forward_line',
                                                     'invalid_forward_allow'}


def test_validation():
    assert codes(fdoc()) == set()
    assert 'forward_member' in codes(fdoc(fwd(target='gone')))
    # The entry's own listeners: the mesh's ports, the proxy's and the forward tunnels'.
    assert 'forward_port_conflict' in codes(fdoc(fwd(entry='r1', listen=52000)))
    assert 'forward_port_conflict' in codes(fdoc(fwd(entry='r1', listen=51999, proto='udp')))
    assert 'forward_port_conflict' not in codes(fdoc(fwd(entry='r1', listen=51999, proto='tcp')))
    assert 'forward_port_conflict' not in codes(fdoc(fwd(listen=51082))), 'a dormant target tunnel does not reserve the entry port'
    assert 'forward_port_conflict' in codes(fdoc(fwd(listen=51080)))
    # Two forwards on one entry: the same port only for different protocols.
    assert 'forward_port_duplicate' in codes(fdoc(fwd(), fwd(id='fw2', proto='tcp')))
    assert 'forward_port_duplicate' not in codes(fdoc(fwd(proto='tcp'), fwd(id='fw2', proto='udp')))
    assert 'forward_port_duplicate' not in codes(fdoc(fwd(), fwd(id='fw2', entry='m2')))
    assert 'forward_port_duplicate' not in codes(fdoc(fwd(), fwd(id='fw2', enabled=False)))
    assert 'forward_fast_self' in codes(fdoc(fwd(target='m1', line='fast')))
    assert codes(fdoc(fwd(target='m1'))) == set()

    d = fdoc()
    d['settings']['forward'] = {'port': 52000, 'fastPort': 52000}
    assert 'forward_port_conflict' not in codes(d)
    assert build(d, ffacts(), 'net', 1)['r2']['forward']['port'] == 52001

    f = ffacts()
    f['r2']['features'] = frozenset({'mesh', mesh_fast.FAST_FEATURE})
    assert 'forward_member_needs_update' in codes(fdoc(), f)
    f['r2'].pop('features')
    assert 'forward_member_needs_update' not in codes(fdoc(), f)
    assert 'forward_member_needs_update' in codes(fdoc(), f, applying=True)
    assert {'forward_member_needs_update', 'forward_geo_missing'} <= model.APPLY_ERRORS


def test_province_data_needed_at_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh_geo, 'STORE', mesh_geo.Store(tmp_path))
    d = fdoc(fwd(allow={'prefixes': [], 'provinces': ['广东省']}))
    assert 'forward_geo_missing' not in codes(d)
    assert 'forward_geo_missing' in codes(d, applying=True)
    mesh_geo.STORE.sets = {'广东省': ['1.0.8.0/21']}
    assert 'forward_geo_missing' not in codes(d, applying=True)


def test_mesh_line_rules_and_configs(geo):
    d = fdoc(fwd(proxyProtocol=2, allow={'prefixes': ['203.0.113.0/24'], 'provinces': ['广东省']}),
             fwd(id='fw2', entry='m2', listen=8080, proto='tcp', host='10.1.2.3', port=80), fwd(id='fw3', listen=9000, enabled=False))
    f = ffacts()
    by_id = {m['serverId']: m for m in d['members']}
    addr = {sid: m['address'] for sid, m in by_id.items()}
    assert model.validate(d, f) == []
    rule = next(r for r in node_rules(d, 'r2', f) if r['ports'] == [[51082, 51082]])
    assert rule == {'src': sorted([f"{addr['m1']}/32", f"{addr['m2']}/32"]), 'dst': [f"{addr['r2']}/32"], 'proto': 'tcp', 'ports': [[51082, 51082]]}
    assert not any(r['ports'] == [[51082, 51082]] for r in node_rules(d, 'm1', f))

    configs = build(d, f, 'net', 1)
    assert configs['m1']['forward'] == {
        'port': 51082,
        'forwards': [{'id': 'fw1', 'listen': 443, 'proto': 'any', 'node': addr['r2'], 'host': '127.0.0.1', 'port': 8443, 'proxyProtocol': 2,
                      'allow': ['203.0.113.0/24'], 'allowSets': ['province:广东省']}],
        'sets': {'province:广东省': ['1.0.8.0/21', '2408:8000::/20']}}
    assert configs['m2']['forward']['forwards'] == [{'id': 'fw2', 'listen': 8080, 'proto': 'tcp', 'node': addr['r2'], 'host': '10.1.2.3', 'port': 80}]
    target = configs['r2']['forward']
    assert target == {'port': 51082, 'targets': sorted([{'from': addr['m1'], 'host': '127.0.0.1', 'port': 8443, 'proto': 'any'},
                                                         {'from': addr['m2'], 'host': '10.1.2.3', 'port': 80, 'proto': 'tcp'}],
                                                        key=lambda t: tuple(sorted(t.items())))}
    assert 'forward' not in configs['r1']
    # A mesh line makes no fast relay.
    assert all('fast' not in c for c in configs.values())


def test_fast_line_makes_a_fast_port_for_its_entries(geo):
    d = fdoc(fwd(line='fast'), fwd(id='fw2', entry='m2', listen=443, line='fast'))
    f = ffacts()
    addr = {m['serverId']: m['address'] for m in d['members']}
    assert model.validate(d, f) == []
    configs = build(d, f, 'net', 1)
    assert configs['m1']['forward']['fastPort'] == 51083 and configs['m1']['forward']['forwards'][0]['fast'] is True
    assert configs['r2']['forward']['fastPort'] == 51083 and all(t['fast'] for t in configs['r2']['forward']['targets'])
    # The target listens on the relay port; each entry takes its connections to the fast forward port.
    assert configs['r2']['fast']['port'] == 52003
    src = sorted([f"{addr['m1']}/32", f"{addr['m2']}/32"])
    for sid in ('m1', 'm2'):
        assert {'node': 'r2', 'port': 51083, 'proto': 'tcp', 'src': src} in configs[sid]['fast']['targets'] and configs[sid]['fast']['capture']
    # Others are not among its sources: they take nothing to it.
    assert not any(t['port'] == 51083 for t in configs['r1']['fast'].get('targets', []))
    rule = next(r for r in node_rules(d, 'r2', f) if r['ports'] == [[51083, 51083]])
    assert rule['src'] == src and rule['proto'] == 'tcp'
    assert not any(r['ports'] == [[51082, 51082]] for r in node_rules(d, 'r2', f))
    # Turned off, the fast port goes with it.
    off = copy.deepcopy(d)
    for x in off['forwards']:
        x['enabled'] = False
    assert all('fast' not in c and 'forward' not in c for c in build(off, f, 'net', 1).values())


def test_older_nodes_get_no_forward():
    d = fdoc(fwd(line='fast'))
    f = ffacts()
    f['m1']['features'] = frozenset({'mesh', mesh_fast.FAST_FEATURE})
    configs = build(d, f, 'net', 1)
    assert all('forward' not in c for c in configs.values())
    assert not any(t['port'] == 51083 for c in configs.values() for t in (c.get('fast') or {}).get('targets', []))


def test_member_removal_takes_its_forwards_and_undo_brings_them_back():
    d = fdoc(fwd(), fwd(id='fw2', entry='m2', target='r1', listen=8080))
    before = copy.deepcopy(d)
    model.strip(d, 'r2')
    assert [x['id'] for x in d['forwards']] == ['fw2']
    model.unstrip(d, before, 'r2')
    assert [x['id'] for x in d['forwards']] == ['fw1', 'fw2']


def test_clean_status_keeps_forwards():
    raw = {'schema': 1, 'pid': 1, 'ready': True, 'peers': [],
           'forward': {'forwards': [{'id': 'fw1', 'listen': ['tcp [::]:443'], 'errors': ['udp: address already in use'], 'active': 2, 'sessions': 1,
                                     'opened': 9, 'refused': 3, 'failed': 1, 'bytesIn': 100, 'bytesOut': 2000, 'lastError': 'target_refused',
                                     'lastErrorAt': 5, 'ready': 2, 'rttMs': 31.25, 'junk': 'x'},
                                    {'id': 'fw2', 'listen': [], 'via': 'fast'}, {'id': 'fw3', 'listen': [], 'via': 'elsewhere'}],
                       'target': {'listen': ['100.96.0.2:51082'], 'active': 2, 'opened': 9, 'refused': 0, 'failed': 1, 'bytesIn': 100, 'bytesOut': 2000}}}
    out = mesh_status.clean_status(raw)['forward']
    f = out['forwards'][0]
    assert (f['id'], f['listen'], f['errors'], f['active'], f['sessions'], f['refused'], f['lastError'], f['rttMs']) == \
        ('fw1', ['tcp [::]:443'], ['udp: address already in use'], 2, 1, 3, 'target_refused', 31.2)
    assert 'junk' not in f and out['target']['listen'] == ['100.96.0.2:51082'] and out['target']['bytesOut'] == 2000
    # A fast forward's line: only what the node can say.
    assert 'via' not in f and out['forwards'][1]['via'] == 'fast' and 'via' not in out['forwards'][2]
    assert mesh_status.clean_status({'schema': 1})['forward'] is None
    # The page reads it from each node's summary.
    assert mesh_status.summary(mesh_status.clean_status(raw))['forward'] == out
    assert 'forward' not in mesh_status.summary(mesh_status.clean_status({'schema': 1}))


# ----- province data -----
def xdb(segments, version=4):
    """An xdb file of (first, last, region) segments, without the vector index nothing here reads."""
    width = 4 if version == 4 else 16
    regions = b''
    pointers = []
    for _, _, region in segments:
        pointers.append((mesh_geo.HEADER + len(regions), len(region.encode())))
        regions += region.encode()
    start = mesh_geo.HEADER + len(regions)
    blocks = b''
    for (first, last, _), (ptr, length) in zip(segments, pointers):
        if version == 4:
            blocks += struct.pack('<II', first, last)
        else:
            blocks += first.to_bytes(16, 'big') + last.to_bytes(16, 'big')
        blocks += struct.pack('<HI', length, ptr)
    end = start + len(blocks) - (2 * width + 6)
    header = struct.pack('<HHIIIHH', 3, 1, 0, start, end, version, 4).ljust(mesh_geo.HEADER, b'\0')
    return header + regions + blocks


def ip4(s):
    a, b, c, d = map(int, s.split('.'))
    return a << 24 | b << 16 | c << 8 | d


V4 = xdb([(ip4('1.0.1.0'), ip4('1.0.3.255'), '中国|福建省|福州市|中国电信|CN'), (ip4('1.0.8.0'), ip4('1.0.15.255'), '中国|广东省|广州市|中国电信|CN'),
          (ip4('1.0.16.0'), ip4('1.0.31.255'), 'Japan|Tokyo|0|0|JP'), (ip4('1.0.32.0'), ip4('1.0.63.255'), '中国|广东省|广州市|中国电信|CN'),
          (ip4('1.0.64.0'), ip4('1.0.64.4'), '中国|广东省|深圳市|中国联通|CN'), (ip4('2.0.0.0'), ip4('2.0.0.255'), '中国|0|0|0|CN')])
V6 = xdb([(0x24080000 << 96, ((0x24080fff + 1) << 96) - 1, '中国|广东省|0|中国联通|CN'), (0x240a << 112, (0x240b << 112) - 1, '中国|广州|0|0|CN')], 6)


def test_xdb_provinces():
    assert mesh_geo.provinces(V4, 4) == {'福建省': ['1.0.1.0/24', '1.0.2.0/23'],
                                         '广东省': ['1.0.8.0/21', '1.0.32.0/19', '1.0.64.0/30', '1.0.64.4/32']}
    # A city where the province goes is left out.
    assert mesh_geo.provinces(V6, 6) == {'广东省': ['2408::/20']}
    with pytest.raises(mesh_geo.GeoError):
        list(mesh_geo.segments(b'\0' * 10))


class FakeHTTP:
    """Answers the two source URLs with files, or 304 to a request carrying their tag."""

    def __init__(self, files):
        self.files, self.calls = files, []

    def get(self, url, headers, timeout):
        key = next(k for k, v in mesh_geo.SOURCES.items() if v == url)
        self.calls.append((key, headers.get('If-None-Match')))
        body, tag = self.files[key]
        status = 304 if headers.get('If-None-Match') == tag else 200
        return FakeResponse(status, body, tag)


class FakeResponse:
    def __init__(self, status, body, tag):
        self.status, self.headers = status, {'ETag': tag}
        self.content = SimpleNamespace(iter_chunked=self._chunks)
        self.body = body

    async def _chunks(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_store_downloads_what_changed(tmp_path):
    http = FakeHTTP({'v4': (V4, '"a"'), 'v6': (V6, '"b"')})
    store = mesh_geo.Store(tmp_path)
    assert store.due(1000)
    assert await store.refresh(http, 1000) is True
    assert store.prefixes('广东省')[-1] == '2408::/20' and store.prefixes('北京市') is None
    view = store.view()
    gd = next(p for p in view['provinces'] if p['name'] == '广东省')
    assert view['ready'] and (gd['v4'], gd['v6']) == (4, 1) and view['updatedAt'] == 1000

    # Unchanged: one conditional request each, nothing made again.
    http.calls.clear()
    assert not store.due(1000 + 3600_000) and store.due(1000 + mesh_geo.CHECK_S * 1000)
    assert await store.refresh(http, 2000) is False
    assert http.calls == [('v4', '"a"'), ('v6', '"b"')] and store.meta['updatedMs'] == 1000

    # From disk after a restart.
    again = mesh_geo.Store(tmp_path)
    again.load()
    assert again.sets == store.sets and again.meta['etags'] == {'v4': '"a"', 'v6': '"b"'}

    # One file changed: both are read again, and the sets follow.
    http.files['v4'] = (xdb([(ip4('1.0.8.0'), ip4('1.0.8.255'), '中国|广东省|广州市|中国电信|CN')]), '"c"')
    assert await store.refresh(http, 3000) is True
    assert store.prefixes('广东省') == ['1.0.8.0/24', '2408::/20'] and store.prefixes('福建省') is None


async def test_store_keeps_what_it_has_when_a_download_fails(tmp_path):
    store = mesh_geo.Store(tmp_path)
    store.sets = {'广东省': ['1.0.8.0/21']}
    broken = FakeHTTP({'v4': (V4, '"a"'), 'v6': (V6, '"b"')})
    broken.get = lambda url, headers, timeout: FakeResponse(503, b'', '')
    assert await store.refresh(broken, 5000) is False
    assert store.sets == {'广东省': ['1.0.8.0/21']} and 'HTTP 503' in store.meta['error']
    assert not store.due(5000 + 60_000) and store.due(5000 + mesh_geo.RETRY_S * 1000)


async def test_controller_fetches_while_a_forward_uses_a_province(tmp_path, monkeypatch):
    store = mesh_geo.Store(tmp_path)
    monkeypatch.setattr(mesh_geo, 'STORE', store)
    docs = {('o1', 'draft'): fdoc(), ('o1', 'applied'): fdoc(), ('o2', 'draft'): fdoc(fwd(allow={'prefixes': [], 'provinces': ['广东省']})),
            ('o2', 'applied'): fdoc()}
    reconciled = []

    async def load(owner, stage):
        return docs[(owner, stage)]
    http = FakeHTTP({'v4': (V4, '"a"'), 'v6': (V6, '"b"')})
    fake = SimpleNamespace(owners={'s1': 'o1', 's2': 'o2'}, _load=load, _reconcile_soon=reconciled.append, service=SimpleNamespace(http=http))
    assert await Mesh._geo_check(fake) is True
    assert reconciled == ['o2'] and store.prefixes('广东省')

    # Nobody uses a province: nothing is downloaded.
    store2 = mesh_geo.Store(tmp_path / 'other')
    monkeypatch.setattr(mesh_geo, 'STORE', store2)
    docs[('o2', 'draft')] = fdoc()
    http.calls.clear()
    assert await Mesh._geo_check(fake, force=True) is False and http.calls == []


def test_servers_tool_patches_forwards_by_id_and_server_name():
    from app.tools.servers import ServersTool
    body = {'draftVersion': 'x', 'patch': {'forwards': {'upsert': [{'entry': '广东', 'target': 'HK', 'listen': 443, 'port': 443}]}}}
    resolved = ServersTool._resolve_draft_refs(body, {'广东': 's-gd', 'HK': 's-hk'}.get)
    assert resolved['patch']['forwards']['upsert'] == [{'entry': 's-gd', 'target': 's-hk', 'listen': 443, 'port': 443}]
    view = {'forwards': [fwd(note='old'), fwd(id='fw2', listen=80)]}
    out = ServersTool._apply_mesh_patch(view, {'forwards': {'remove': ['fw2'], 'upsert': [{'id': 'fw1', 'note': 'new'}, {'entry': 'm2', 'target': 'r1', 'listen': 22, 'port': 22}]}})
    assert [(x.get('id'), x['note'] if 'note' in x else None) for x in out['forwards']] == [('fw1', 'new'), (None, None)]
