"""Fast relay in the mesh document: fast ports, settings.fast, drafts from before them, the rules
they open, every node's part (listening, targets, direct dials, port mappings, fronts), older
nodes passed over, and status cleaning."""
from __future__ import annotations

import copy

from app.servers import mesh_fast, mesh_model as model, mesh_status
from app.servers.mesh_config import build, node_rules
from tests.test_mesh_config import doc, errors, facts, member, pair


def ffacts(**kw):
    """facts with every node of a release that has fast ports."""
    f = facts(**kw)
    for sid in f:
        f[sid]['features'] = frozenset({'mesh', mesh_fast.FAST_FEATURE})
    return f


def _fast_doc(*ports):
    d = doc(
        member('r1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0}),
        member('r2', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0}),
        member('m1', 'member', entry={'mode': 'failover', 'autoReturn': True, 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'tcp'}]}),
        member('m2', 'member', entry={'mode': 'failover', 'autoReturn': True, 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}]}),
    )
    d['members'][1]['publicPorts'] = {'fast': 62003}
    d['fastPorts'] = list(ports) or [{'id': 'f1', 'serverId': 'r2', 'port': 48888, 'proto': 'any', 'src': dict(mesh_fast.ALL_SOURCES), 'note': ''}]
    mesh_fast.ensure_secrets(d)
    return d


def err_codes(d, f=None, applying=False):
    return {code for _, code in errors(d, f or ffacts(), applying)}


def test_defaults_old_drafts_ports_and_key_rotation():
    d = model.empty_doc()
    assert d['settings']['fast'] == {'port': 52003, 'paths': 3, 'keys': []} and d['fastPorts'] == []

    old = _fast_doc()
    old['settings']['fast'] = {'port': 52003, 'prefix': '100.97.0.0/16', 'keys': []}
    old.pop('fastPorts')
    old['members'][0]['fast'] = True
    old['members'][1]['proxy'] = {'exit': None, 'ports': [{'id': 'p1', 'mode': 'fast', 'port': 1085}, {'id': 'p2', 'mode': 'exit', 'port': 1080}]}
    model.upgrade(old)
    assert old['settings']['fast'] == {'port': 52003, 'paths': 3, 'keys': []} and old['fastPorts'] == []
    assert 'fast' not in old['members'][0]
    assert old['members'][1]['proxy']['ports'] == [{'id': 'p2', 'port': 1080}]

    cleaned = model.clean_doc({'fastPorts': [{'serverId': 'r1', 'port': 443}, {'serverId': 'r2', 'port': 8388, 'proto': 'tcp',
                                                'src': {'members': ['m1']}}], 'settings': {'fast': {'paths': 2}}}, _fast_doc())
    assert [(p['id'], p['proto'], p['src']['all']) for p in cleaned['fastPorts']] == [('f1', 'any', True), ('f2', 'tcp', False)]
    assert cleaned['settings']['fast']['paths'] == 2 and cleaned['settings']['fast']['keys']

    d2 = _fast_doc()
    k1 = d2['settings']['fast']['keys'][0]
    keys = model.clean_doc({'settings': {'fast': {'rotateKey': True}}}, d2)['settings']['fast']['keys']
    assert len(keys) == 2 and keys[1] == k1 and keys[0] != k1


def test_fast_port_validation():
    assert err_codes(_fast_doc()) == set()
    port = {'id': 'f1', 'serverId': 'r2', 'port': 48888, 'proto': 'any', 'src': dict(mesh_fast.ALL_SOURCES), 'note': ''}

    assert 'fast_port_member' in err_codes(_fast_doc({**port, 'serverId': 'gone'}))
    assert 'fast_port_conflict' in err_codes(_fast_doc({**port, 'port': 52000}))
    assert 'fast_port_conflict' in err_codes(_fast_doc({**port, 'port': 52003}))
    merged = _fast_doc(port, {**port, 'id': 'f2', 'proto': 'udp'})
    assert 'fast_port_duplicate' not in err_codes(merged)
    assert build(merged, ffacts(), 'net', 1)['m1']['fast']['targets'] == [{'node': 'r2', 'port': 48888, 'proto': 'any'}]
    assert 'fast_port_duplicate' not in err_codes(_fast_doc({**port, 'proto': 'tcp'}, {**port, 'id': 'f2', 'proto': 'udp'}))

    d = _fast_doc()
    d['settings']['fast']['port'] = 52000
    assert 'fast_port_conflict' in err_codes(d)
    d = _fast_doc()
    d['members'][0]['listen']['fast'] = 51999
    assert {'port_duplicate', 'fast_port_conflict'} & err_codes(d)

    try:
        model.clean_doc({'fastPorts': [{'serverId': 'r1', 'port': 0, 'proto': 'sctp'}]}, _fast_doc())
    except model.MeshInvalid as e:
        assert {x['error'] for x in e.errors} >= {'invalid_port', 'invalid_proto'}
    else:
        raise AssertionError('a bad fast port was accepted')


def test_rules_open_the_port_and_the_relay_port():
    d = _fast_doc()
    f = ffacts()
    by_id = {m['serverId']: m for m in d['members']}
    r2 = node_rules(d, 'r2', f)
    members = sorted(f"{m['address']}/32" for m in d['members'])
    fast_rule = next(r for r in r2 if r['ports'] == [[48888, 48888]])
    assert fast_rule['proto'] == 'any' and fast_rule['dst'] == [f"{by_id['r2']['address']}/32"] and sorted(fast_rule['src']) == members
    relay_rule = next(r for r in r2 if r['ports'] == [[52003, 52003]])
    assert relay_rule['proto'] == 'tcp' and f"{by_id['m1']['address']}/32" in relay_rule['src']
    # m1 neither relays nor has a fast port: it does not listen, so no rule for the relay port.
    assert not any(r['ports'] == [[52003, 52003]] for r in node_rules(d, 'm1', f))


def test_config_listening_targets_sources_direct_dials_and_fronts():
    d = _fast_doc({'id': 'f1', 'serverId': 'm2', 'port': 8388, 'proto': 'tcp', 'src': {**mesh_fast.ALL_SOURCES, 'all': False, 'allSubnets': False, 'members': ['m1']}, 'note': ''},
                  {'id': 'f2', 'serverId': 'r2', 'port': 48888, 'proto': 'any', 'src': dict(mesh_fast.ALL_SOURCES), 'note': ''})
    f = ffacts(platforms={'m2': 'windows'})
    # m1 is behind home NAT (its ipv4 203.0.113.3 is not on its NICs and it has no TCP listener).
    f['m1']['localAddresses'] = ['10.10.10.251/24']
    by_id = {m['serverId']: m for m in d['members']}
    assert model.validate(d, f) == []
    configs = build(d, f, 'net', 1)

    c_r1 = configs['r1']['fast']
    assert c_r1['port'] == 52003 and configs['r1']['self']['listen']['fast'] == 52003 and c_r1['paths'] == 3
    assert c_r1['next']['r2'] == f"{f['r2']['ipv4']}:62003"
    assert set(c_r1['nodes']) == {'r1', 'r2', 'm1', 'm2'}
    # r1 is not among f1's sources: it takes connections to r2's port alone.
    assert c_r1['targets'] == [{'node': 'r2', 'port': 48888, 'proto': 'any'}] and c_r1['capture'] is True

    c_m1 = configs['m1']['fast']
    assert 'port' not in c_m1 and 'fast' not in configs['m1']['self']['listen']
    assert c_m1['next'] == {'r1': f"{f['r1']['ipv4']}:52003"}
    assert c_m1['targets'] == [{'node': 'm2', 'port': 8388, 'proto': 'tcp', 'src': [f"{by_id['m1']['address']}/32"]},
                               {'node': 'r2', 'port': 48888, 'proto': 'any'}]

    # m2 listens for its own fast port; r2's port is its only target.
    c_m2 = configs['m2']['fast']
    assert c_m2['port'] == 52003 and c_m2['targets'] == [{'node': 'r2', 'port': 48888, 'proto': 'any'}]

    # No fast port, no fast relay anywhere.
    d0 = copy.deepcopy(d)
    d0['fastPorts'] = []
    assert all('fast' not in c for c in build(d0, f, 'net', 1).values())

    # A CN front dials its exit's dedicated address, and the exit never dials it.
    d_front = copy.deepcopy(d)
    d_front['relayLinks'] = {'mode': 'custom', 'pairs': [pair('r1', 'r2', transport='tls', dialer='a')]}
    d_front['limits'] = [{
        'id': 'k1', 'name': 'CN-outbound', 'on': True,
        'a': {'kind': 'region', 'regions': ['CN'], 'not': False},
        'b': {'kind': 'region', 'regions': ['CN'], 'not': True},
        'relayEnd': False, 'transports': ['tcp', 'tls'], 'via': None, 'manualOnly': True,
    }]
    f_front = copy.deepcopy(f)
    f_front['r1']['region'] = 'CN'
    f_front['r1']['front'] = [{'serverId': 'r2', 'address': '10.99.0.2'}]
    f_front['r2']['region'] = 'JP'
    cfgs_front = build(d_front, f_front, 'net', 1)
    assert cfgs_front['r1']['fast']['next']['r2'] == '10.99.0.2:52003'
    assert 'r1' not in cfgs_front['r2']['fast'].get('next', {})
    assert ['r1', 'r2'] in cfgs_front['r1']['fast']['dial'] and ['r2', 'r1'] not in cfgs_front['r1']['fast']['dial']


def test_older_nodes_passed_over_and_flagged_at_apply():
    d = _fast_doc()
    f = ffacts()
    f['r1']['features'] = frozenset({'mesh', 'mesh.fast'})
    assert 'fast_member_needs_update' not in err_codes(d, f, applying=True)
    configs = build(d, f, 'net', 1)
    assert 'fast' not in configs['r1'] and 'r1' not in configs['m1']['fast']['nodes']
    assert 'r1' not in configs['r2']['fast'].get('next', {})

    f['r2']['features'] = frozenset({'mesh'})
    assert 'fast_member_needs_update' in err_codes(d, f, applying=True)
    assert 'fast_member_needs_update' in model.APPLY_ERRORS
    assert all('fast' not in c for c in build(d, f, 'net', 1).values())


def test_clean_status_keeps_capture_and_channels():
    raw = {
        'schema': 1, 'pid': 100, 'version': '1.3.1', 'ready': True, 'peers': [],
        'fast': {
            'port': 52003, 'listenError': '',
            'capture': {'mode': 'nft', 'active': False, 'udp': False, 'error': 'nft not found', 'missing': 'nft'},
            'channels': [{'dest': 'r2', 'ready': True, 'using': 1, 'opened': 1, 'lastUse': 2, 'streams': 5, 'fallbacks': 1,
                          'paths': [{'hops': ['r1', 'r2'], 'state': 'down', 'since': 3, 'error': 'refused'},
                                    {'hops': ['r2'], 'fixed': 'p1', 'state': 'ready', 'since': 4, 'rttMs': 12.4}]}],
            'entry': {'active': 2, 'opened': 10, 'failed': 0, 'bytesIn': 1024, 'bytesOut': 2048},
            'transit': {'active': 1, 'opened': 5, 'failed': 0, 'bytesIn': 4096, 'bytesOut': 4096},
            'exit': {'active': 0, 'opened': 3, 'failed': 1, 'bytesIn': 512, 'bytesOut': 8192},
            'peers': [{'peer': 'r2', 'target': '203.0.113.2:62003', 'viaMesh': False, 'ready': 4, 'rttMs': 12.4, 'down': False, 'error': ''}],
            'headerErrors': 0, 'refusedEntry': 1, 'refusedExit': 2,
            'tcpMem': {
                'pageBytes': 4096,
                'original': [9500, 12600, 19000],
                'current': [40000, 48000, 60000],
                'originalBytes': [38912000, 51609600, 77824000],
                'currentBytes': [163840000, 196608000, 245760000],
                'changed': True, 'limited': False, 'readOnly': False, 'container': False, 'pressures': 3, 'error': '',
            },
        },
    }
    cleaned = mesh_status.clean_status(raw)
    fast = cleaned['fast']
    assert fast['capture'] == {'mode': 'nft', 'active': False, 'udp': False, 'error': 'nft not found', 'missing': 'nft'}
    ch = fast['channels'][0]
    assert ch['dest'] == 'r2' and ch['ready'] and ch['using'] == 1 and ch['streams'] == 5 and ch['fallbacks'] == 1
    assert ch['paths'][0]['state'] == 'down' and ch['paths'][1] == {'hops': ['r2'], 'fixed': 'p1', 'state': 'ready', 'since': 4, 'rttMs': 12.4, 'error': ''}
    assert fast['tcpMem']['pressures'] == 3 and fast['peers'][0]['rttMs'] == 12.4
    assert 'address' not in fast and 'transparent' not in fast
    summ = mesh_status.summary(cleaned, 'relay')
    assert summ['fast']['channels'][0]['dest'] == 'r2'
