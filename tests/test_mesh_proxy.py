"""The exit proxy in the mesh document: exits with their own proxy, exit groups, proxy ports,
what each node's config says, the rules that let members reach exits, and documents from before."""
from __future__ import annotations

import copy

import pytest

from app.servers import mesh_proxy
from app.servers.mesh import Mesh
from app.servers import mesh_model as model
from app.servers.mesh_config import build, node_rules
from tests.test_mesh_config import doc, errors, facts, member


def exit_(user, **x):
    return {'user': user, 'password': f'{user}-pw', 'port': 1081, 'subnets': False, 'lan': False, 'interface': '', 'source': '', **x}


def port(pid='p1', **p):
    return {'id': pid, 'listen': 'local', 'address': '', 'port': 1080, 'exit': '', 'group': '', 'select': 'order', 'pinned': '', 'strict': False,
            'user': 'proxy', 'password': '', **p}


def proxied():
    """r1 relays; m1 and r2 are exits in the home group; m2 has a port to home and one to r2 alone,
    r2 a LAN port to home."""
    d = doc(member('r1', 'relay', listen={'tcp': 52000}), member('r2', 'relay'), member('m1'), member('m2'))
    by = {m['serverId']: m for m in d['members']}
    by['m1']['proxy'] = {'exit': exit_('m1'), 'ports': []}
    by['m2']['proxy'] = {'exit': None, 'ports': [port(group='home'), port('p2', port=1082, exit='r2')]}
    by['r2']['proxy'] = {'exit': exit_('r2'), 'ports': [port(listen='lan', address='192.168.1.10', group='home', strict=True, password='lan-pw')]}
    d['exitGroups'] = [{'id': 'home', 'name': '家宽', 'members': ['m1', 'r2']}]
    return d


def addresses(d):
    return {m['serverId']: m['address'] for m in d['members']}


def test_a_proxy_change_is_named_in_the_draft_changes():
    applied, draft = proxied(), proxied()
    draft['members'][2]['proxy'] = None
    changes = Mesh._changes(applied, draft, facts())
    assert [c['text'] for c in changes if c['kind'] == 'member.update'] == ['修改 M1 的出口代理']


def test_a_proxied_draft_has_no_errors_and_old_documents_gain_the_defaults():
    assert errors(proxied()) == set()
    old = proxied()
    del old['settings']['proxy']
    for m in old['members']:
        del m['proxy']
    model.upgrade(old)
    assert old['settings']['proxy'] == {'port': 51080, 'password': ''} and all(m['proxy'] is None for m in old['members'])


def test_an_exit_serves_its_own_proxy_on_its_mesh_address():
    d = proxied()
    a = addresses(d)
    configs = build(d, facts(), 'net', 1)
    assert 'proxy' not in configs['r1']
    # Its own address among the tunnel's clients: its proxy leads to itself through it.
    assert configs['m1']['proxy'] == {'port': 51080, 'exit': {'clients': sorted([a['m1'], a['m2'], a['r2']])},
                                      'entries': [{'name': 'exit', 'listen': [f"{a['m1']}:1081"], 'password': 'm1-pw',
                                                   'users': [{'name': 'm1', 'exits': [a['m1']]}]}]}


def test_proxy_ports_lead_to_their_exit_or_group():
    d = proxied()
    a = addresses(d)
    configs = build(d, facts(), 'net', 1)
    # On loopback alone: no password, every client its one user.
    assert configs['m2']['proxy'] == {'port': 51080, 'entries': [
        {'name': 'p1', 'listen': ['127.0.0.1:1080'], 'password': '', 'users': [{'name': 'proxy', 'exits': [a['m1'], a['r2']]}], 'default': 'proxy'},
        {'name': 'p2', 'listen': ['127.0.0.1:1082'], 'password': '', 'users': [{'name': 'proxy', 'exits': [a['r2']]}], 'default': 'proxy'}]}
    # On the LAN: its own password.
    assert configs['r2']['proxy']['entries'][1] == {'name': 'p1', 'listen': ['127.0.0.1:1080', '192.168.1.10:1080'], 'password': 'lan-pw',
                                                    'users': [{'name': 'proxy', 'exits': [a['m1'], a['r2']]}], 'strict': True}


def test_a_port_picks_its_groups_exits_by_its_select():
    d = proxied()
    a = addresses(d)
    p = d['members'][3]['proxy']['ports'][0]
    p.update(select='manual', pinned='r2')
    assert build(d, facts(), 'net', 1)['m2']['proxy']['entries'][0]['users'] == [{'name': 'proxy', 'exits': [a['r2']]}]
    p.update(select='latency', pinned='')
    assert build(d, facts(), 'net', 1)['m2']['proxy']['entries'][0]['users'] == [{'name': 'proxy', 'exits': [a['m1'], a['r2']], 'select': 'latency'}]


def test_a_node_from_before_entries_runs_its_exits_proxy_and_asks_for_an_update_for_ports():
    d = proxied()
    a = addresses(d)
    f = facts()
    for sid in ('m1', 'm2'):
        f[sid]['features'] = frozenset({'mesh', 'mesh.standby'})
    assert build(d, f, 'net', 1)['m1']['proxy']['entry'] == {'listen': [f"{a['m1']}:1081"], 'password': 'm1-pw',
                                                              'users': [{'name': 'm1', 'exits': [a['m1']]}]}
    assert 'proxy' not in build(d, f, 'net', 1)['m2']
    assert ('members[3].proxy.ports[0]', 'proxy_ports_need_update') in errors(d, f)


def test_a_node_from_before_the_proxy_is_no_exit_and_no_port_leads_to_it():
    d = proxied()
    a = addresses(d)
    f = facts()
    f['m1']['features'] = frozenset({'mesh'})
    configs = build(d, f, 'net', 1)
    assert 'proxy' not in configs['m1']
    assert [u['exits'] for e in configs['m2']['proxy']['entries'] for u in e['users']] == [[a['r2']], [a['r2']]]
    assert ('members[2].proxy.exit', 'proxy_exit_needs_update') in errors(d, f)
    # Kept in the draft, refused at apply: the node is updated in the meantime.
    assert {'proxy_exit_needs_update', 'proxy_ports_need_update'} <= model.APPLY_ERRORS


def test_every_member_reaches_an_exit_and_the_ports_its_tunnel_whatever_the_rules_say():
    d = proxied()
    a = addresses(d)
    assert d['rules'] == []
    key = lambda n: tuple(map(int, n[:-3].split('.')))
    others = sorted([f"{a[x]}/32" for x in ('r1', 'r2', 'm2')], key=key)
    rules = node_rules(d, 'm1', facts())
    for proto in ('tcp', 'udp'):
        assert {'src': others, 'dst': [f"{a['m1']}/32"], 'proto': proto, 'ports': [[1081, 1081]]} in rules
    assert {'src': sorted([f"{a['m2']}/32", f"{a['r2']}/32"], key=key), 'dst': [f"{a['m1']}/32"], 'proto': 'tcp', 'ports': [[51080, 51080]]} in rules
    assert not any(r.get('ports') in ([[1081, 1081]], [[51080, 51080]]) for r in node_rules(d, 'm2', facts()))


def test_an_exit_may_let_the_networks_behind_gateways_in():
    d = proxied()
    a = addresses(d)
    by = {m['serverId']: m for m in d['members']}
    by['m2']['gateway'] = {'subnets': ['192.168.10.0/24'], 'masquerade': False, 'backup': False}
    tcp = lambda: next(r for r in node_rules(d, 'm1', facts()) if r['proto'] == 'tcp' and r['ports'] == [[1081, 1081]])
    assert '192.168.10.0/24' not in tcp()['src']
    by['m1']['proxy']['exit']['subnets'] = True
    assert '192.168.10.0/24' in tcp()['src'] and f"{a['m2']}/32" in tcp()['src']


@pytest.mark.parametrize('change, f, expected', [
    (lambda by, g: by['r2']['proxy']['exit'].update(user='m1'), None, ('members[2].proxy.exit.user', 'proxy_name_duplicate')),
    (lambda by, g: by['m1']['proxy']['exit'].update(port=52000), None, None),
    (lambda by, g: by['r1'].update(proxy={'exit': exit_('r1', port=52000), 'ports': []}), None, ('members[0].proxy.exit.port', 'proxy_port_conflict')),
    (lambda by, g: g[0].update(members=['m1', 'm2']), None, ('exitGroups[0].members[1]', 'proxy_group_not_exit')),
    (lambda by, g: by['m2']['proxy']['ports'][0].update(select='manual', pinned=''), None, ('members[3].proxy.ports[0].pinned', 'proxy_group_pinned')),
    (lambda by, g: by['m2']['proxy']['ports'][0].update(group=''), None, ('members[3].proxy.ports[0]', 'proxy_port_target')),
    (lambda by, g: by['m2']['proxy']['ports'][0].update(exit='r2'), None, ('members[3].proxy.ports[0]', 'proxy_port_target')),
    (lambda by, g: by['m2']['proxy']['ports'][0].update(group='jp'), None, ('members[3].proxy.ports[0].group', 'proxy_port_group')),
    (lambda by, g: by['m2']['proxy']['ports'][1].update(exit='r1'), None, ('members[3].proxy.ports[1].exit', 'proxy_port_not_exit')),
    (lambda by, g: by['m2']['proxy']['ports'][1].update(port=1080), None, ('members[3].proxy.ports[1].port', 'proxy_port_conflict')),
    (lambda by, g: by['m2']['proxy']['ports'][0].update(port=51080), None, ('members[3].proxy.ports[0].port', 'proxy_port_conflict')),
    (lambda by, g: by['m1']['proxy']['exit'].update(interface='eth0'), {'m1': 'windows'}, ('members[2].proxy.exit.interface', 'proxy_interface_requires_linux')),
])
def test_each_proxy_check_names_its_field(change, f, expected):
    d = proxied()
    change({m['serverId']: m for m in d['members']}, d['exitGroups'])
    found = errors(d, facts(platforms=f))
    assert expected in found if expected else found == set()


def test_the_tunnel_port_may_not_be_a_transport_port():
    d = proxied()
    d['settings']['proxy']['port'] = 52000
    assert ('settings.proxy.port', 'proxy_port_conflict') in errors(d)


def test_drafts_are_cleaned_and_exits_and_lan_ports_get_their_secrets():
    d = doc(member('m1'), member('m2'))
    raw = {'members': [{**d['members'][0], 'proxy': {'exit': {'user': 'Edge-EU'}}},
                       {**d['members'][1], 'proxy': {'ports': [{'listen': 'lan', 'address': '192.168.10.250', 'exit': 'm1'}, {'port': 1082, 'exit': 'm1'}]}}],
           'exitGroups': [{'name': ' 美国 ', 'members': ['m1']}]}
    clean = model.clean_doc(raw, d)
    x = clean['members'][0]['proxy']['exit']
    assert x['user'] == 'edge-eu' and len(x['password']) >= 20 and x['port'] == 1081 and not x['subnets']
    assert clean['exitGroups'] == [{'id': 'g1', 'name': '美国', 'members': ['m1']}]
    lan, local = clean['members'][1]['proxy']['ports']
    assert lan['id'] == 'p1' and len(lan['password']) >= 20 and local == port('p2', port=1082, exit='m1')
    # Kept once made.
    again = model.clean_doc(clean, clean)
    assert again['members'][0]['proxy']['exit']['password'] == x['password'] and again['members'][1]['proxy']['ports'][0]['password'] == lan['password']
    # An exit without a username takes its server's.
    assert model.clean_doc({'members': [{**d['members'][0], 'proxy': {'exit': {}}}, d['members'][1]]}, d)['members'][0]['proxy']['exit']['user'] == 'm1'
    with pytest.raises(model.MeshInvalid) as bad:
        model.clean_doc({'members': [{**d['members'][0], 'proxy': {'exit': {'source': 'x', 'user': 'Bad Name'}}},
                                     {**d['members'][1], 'proxy': {'ports': [{'select': 'fastest'}]}}],
                         'exitGroups': [{'name': 'x', 'user': 'us', 'members': ['m1']}]}, d)
    assert {e['path'] for e in bad.value.errors} == {'exitGroups[0]', 'members[1].proxy.ports[0].select', 'members[0].proxy.exit.source', 'members[0].proxy.exit.user'}
    # Turning both off is the member back to no proxy.
    assert model.clean_doc({'members': [{**d['members'][0], 'proxy': {'exit': None, 'ports': []}}, d['members'][1]]}, d)['members'][0]['proxy'] is None


def before_exits():
    """The document of 11-10: four exits each in its own group, and m2's entry on 1081 refusing
    clients without a username; a group of two, and r2's LAN entry defaulting to it."""
    d = doc(member('r1', 'relay', listen={'tcp': 52000}), member('r2', 'relay'), member('m1'), member('m2'))
    by = {m['serverId']: m for m in d['members']}
    old_exit = {'lan': True, 'interface': 'eth0', 'source': ''}
    by['m1']['proxy'] = {'exit': dict(old_exit), 'entry': None}
    by['r1']['proxy'] = {'exit': {'lan': False, 'interface': '', 'source': ''}, 'entry': None}
    by['m2']['proxy'] = {'exit': None, 'entry': {'listen': 'local', 'address': '', 'port': 1081, 'default': '', 'strict': False}}
    by['r2']['proxy'] = {'exit': None, 'entry': {'listen': 'lan', 'address': '192.168.1.10', 'port': 1080, 'default': 'both', 'strict': True}}
    d['exitGroups'] = [{'id': 'Edge-EU', 'name': 'Edge-EU', 'user': 'edge-eu', 'members': ['m1'], 'select': 'order', 'pinned': ''},
                       {'id': 'att', 'name': 'att', 'user': 'att', 'members': ['r1'], 'select': 'order', 'pinned': ''},
                       {'id': 'g1', 'name': 'both', 'user': 'both', 'members': ['m1', 'r1'], 'select': 'latency', 'pinned': ''}]
    d['settings']['proxy']['password'] = 'shared'
    return d


def test_exits_from_before_get_their_groups_username_and_ports_take_the_entries_place():
    d = model.upgrade(before_exits())
    by = {m['serverId']: m for m in d['members']}
    m1 = by['m1']['proxy']['exit']
    assert m1 == {'user': 'edge-eu', 'password': m1['password'], 'port': 1081, 'subnets': False, 'lan': True, 'interface': 'eth0', 'source': ''}
    assert by['r1']['proxy'] == {'exit': {**exit_('att'), 'password': by['r1']['proxy']['exit']['password']}, 'ports': []}
    # Derived from the shared one: the same on every load, each exit its own.
    assert len(m1['password']) == 24 and m1['password'] != by['r1']['proxy']['exit']['password']
    assert model.upgrade(before_exits())['members'][2]['proxy']['exit']['password'] == m1['password']
    # The groups of one exit made for each exit go; the others stay, as plain groups.
    assert d['exitGroups'] == [{'id': 'g1', 'name': 'both', 'members': ['m1', 'r1']}]
    # An entry refusing anonymous clients: a port still to be pointed somewhere.
    assert by['m2']['proxy'] == {'exit': None, 'ports': [port(port=1081)]}
    assert by['r2']['proxy'] == {'exit': None, 'ports': [port(listen='lan', address='192.168.1.10', group='g1', select='latency', strict=True, password='shared')]}
    assert errors(d) == {('members[3].proxy.ports[0]', 'proxy_port_target')}
    # Run again, it changes nothing.
    assert model.upgrade(copy.deepcopy(d)) == d


def test_an_entry_defaulting_to_an_exits_own_group_leads_to_that_exit():
    old = before_exits()
    old['members'][3]['proxy']['entry']['default'] = 'edge-eu'
    assert model.upgrade(old)['members'][3]['proxy']['ports'] == [port(port=1081, exit='m1')]


def test_exits_from_before_groups_become_exits_with_their_usernames():
    d = before_exits()
    del d['exitGroups']
    by = {m['serverId']: m for m in d['members']}
    by['m1']['proxy']['exit'].update(name='home', backups=['r1'])
    by['r1']['proxy']['exit'].update(name='us', backups=[])
    by['m2']['proxy']['entry']['default'] = 'home'
    model.upgrade(d)
    assert [by[x]['proxy']['exit']['user'] for x in ('m1', 'r1')] == ['home', 'us']
    assert d['exitGroups'] == [{'id': 'home', 'name': 'home', 'members': ['m1', 'r1']}]
    assert by['m2']['proxy']['ports'][0]['group'] == 'home'


def removing(d, sid):
    """d as removing sid leaves it (Mesh.remove): marked, and what pointed at it stripped."""
    before = copy.deepcopy(d)
    next(m for m in d['members'] if m['serverId'] == sid)['removing'] = True
    return model.strip(d, sid), before


def test_removing_an_exit_takes_it_out_of_its_groups_and_ports():
    """From the 11-09 review: the removed exit stayed in its groups (proxy_group_not_exit), so no draft applied."""
    d, _ = removing(proxied(), 'm1')
    assert d['exitGroups'] == [{'id': 'home', 'name': '家宽', 'members': ['r2']}]
    assert errors(d) == set()
    d, _ = removing(proxied(), 'r2')
    assert d['exitGroups'][0]['members'] == ['m1'] and d['members'][3]['proxy']['ports'][1]['exit'] == ''
    assert errors(d) == {('members[3].proxy.ports[1]', 'proxy_port_target')}


def test_a_port_pinned_to_a_removed_exit_asks_for_another():
    pinned = proxied()
    pinned['members'][3]['proxy']['ports'][0].update(select='manual', pinned='m1')
    d, _ = removing(pinned, 'm1')
    assert d['members'][3]['proxy']['ports'][0]['pinned'] == ''
    assert errors(d) == {('members[3].proxy.ports[0].pinned', 'proxy_group_pinned')}


def test_undoing_an_exit_removal_brings_back_its_groups_pins_and_ports():
    start = proxied()
    start['members'][3]['proxy']['ports'][0].update(select='manual', pinned='r2')
    d, before = removing(copy.deepcopy(start), 'r2')
    d['members'][1]['removing'] = False
    assert model.unstrip(d, before, 'r2') == start


def test_a_nodes_entries_are_reported_by_name():
    from app.servers.mesh_status import _proxy
    out = _proxy({'entries': [{'name': 'exit', 'listen': ['100.96.0.2:1081'], 'rejected': 2,
                               'users': [{'name': 'm1', 'opened': 3, 'exits': [{'address': '100.96.0.2', 'ready': 2}]}]}]})
    assert out['entries'][0]['name'] == 'exit' and out['entries'][0]['rejected'] == 2
    assert out['entries'][0]['users'][0]['opened'] == 3 and out['entries'][0]['users'][0]['exits'][0]['ready'] == 2
