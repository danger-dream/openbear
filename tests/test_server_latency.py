"""延迟监测 rules: settings, a server's own targets, the directory, and what each kind of server measures."""
from __future__ import annotations

import pytest

from app.instances.contracts import InstanceError
from app.servers import latency as lat
from app.servers.route import retier


def test_settings_rules():
    assert lat.clean_settings({}) == lat.DEFAULTS and lat.DEFAULTS['province'] == '云南' and lat.DEFAULTS['intervalS'] == 10
    assert lat.DEFAULTS['defaults'] == {'domestic': ['three', 'ali-dns', 'tencent-dns'],
                                        'optimized': ['three', 'cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram'],
                                        'plain': ['cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram']}
    value = lat.clean_settings({'province': '北京', 'intervalS': 3, 'method': 'icmp', 'card': 'three', 'defaults': {'domestic': ['114-dns', 'three'], 'plain': []}})
    assert value == {**lat.DEFAULTS, 'province': '北京', 'intervalS': 3, 'method': 'icmp', 'card': 'three',
                     'defaults': {**lat.DEFAULTS['defaults'], 'domestic': ['114-dns', 'three'], 'plain': []}}
    for bad, code in (({'intervalS': 2}, 'invalid_latency_interval'), ({'intervalS': 601}, 'invalid_latency_interval'),
                      ({'intervalS': 10.5}, 'invalid_latency_interval'), ({'province': '台湾'}, 'invalid_latency_province'),
                      ({'method': 'udp'}, 'invalid_latency_settings'), ({'defaults': {'domestic': ['nope']}}, 'invalid_latency_targets'),
                      ({'defaults': {'domestic': ['qq', 'qq']}}, 'invalid_latency_targets'), ({'defaults': {'abroad': []}}, 'invalid_latency_targets'),
                      ({'card': 'best'}, 'invalid_latency_settings'), ({'cn': []}, 'invalid_latency_settings'),
                      ({'enabled': 1}, 'invalid_latency_settings'), ({'x': 1}, 'invalid_latency_settings')):
        with pytest.raises(InstanceError) as raised:
            lat.clean_settings(bad)
        assert raised.value.error == code


def test_the_users_own_addresses():
    value = lat.clean_settings({'custom': [{'label': ' 家里 NAS ', 'host': 'NAS.Example.com', 'port': 443, 'group': 'cn'},
                                           {'label': '东京 VPS', 'host': '203.0.113.9', 'port': 22, 'group': 'intl'}]})
    home, tokyo = value['custom']
    assert lat.CUSTOM_ID.match(home['id']) and home == {'id': home['id'], 'label': '家里 NAS', 'host': 'nas.example.com', 'port': 443, 'group': 'cn'}
    # Kept by its id when saved again; in a default list like a built-in one.
    again = lat.clean_settings({'custom': [tokyo], 'defaults': {'plain': [tokyo['id'], 'github']}}, value)
    assert again['custom'] == [tokyo] and again['defaults']['plain'] == [tokyo['id'], 'github']
    # Taken out: it leaves the lists with it.
    gone = lat.clean_settings({'custom': []}, again)
    assert gone['custom'] == [] and gone['defaults']['plain'] == ['github']
    for bad in ([{'label': '', 'host': 'a.example', 'port': 1, 'group': 'cn'}], [{'label': 'x', 'host': '127.0.0.1', 'port': 1, 'group': 'cn'}],
                [{'label': 'x', 'host': 'bad host', 'port': 1, 'group': 'cn'}], [{'label': 'x', 'host': 'a.example', 'port': 0, 'group': 'cn'}],
                [{'label': 'x', 'host': 'a.example', 'port': 1, 'group': 'mars'}], [{'label': 'x', 'host': 'a.example', 'port': 1, 'group': 'cn', 'id': 'u-nope'}],
                [{'label': 'x', 'host': 'a.example', 'port': 1, 'group': 'cn', 'extra': 1}], 'x',
                [{'label': f'{i}', 'host': 'a.example', 'port': 1, 'group': 'cn'} for i in range(33)]):
        with pytest.raises(InstanceError) as raised:
            lat.clean_settings({'custom': bad})
        assert raised.value.error == 'invalid_latency_custom'
    # What a server measures takes it by its host and port; one taken out is passed over, and named so.
    lib = lat.library(value)
    found = lat.targets_for(value, {'latency': {'targets': [home['id'], 'u-00000000', 'github']}}, False, lat.builtin_directory(), 52002)
    assert found == [{'id': home['id'], 'host': 'nas.example.com', 'port': 443, 'mode': 'tcp'}, {'id': 'github', 'host': 'github.com', 'port': 443, 'mode': 'tcp'}]
    assert lat.label(home['id'], lib) == '家里 NAS' and lat.label('u-00000000', lib) == '已删除的地址'


def test_settings_from_before_the_library():
    # The old built-in pair abroad becomes today's defaults; lists of one's own are kept.
    old = lat.clean_settings({}, {'enabled': True, 'province': '北京', 'cn': ['ali-dns', 'tencent-dns'], 'abroad': ['cloudflare', 'google']})
    assert 'cn' not in old and old['province'] == '北京' and old['defaults'] == lat.DEFAULTS['defaults']
    own = lat.clean_settings({}, {'cn': ['114-dns'], 'abroad': ['quad9']})
    assert own['defaults'] == {'domestic': ['three', '114-dns'], 'optimized': ['three', 'quad9'], 'plain': ['quad9']}


def test_what_a_card_shows():
    ids = ['yn-ct', 'yn-cm', 'cloudflare', 'openai']
    assert lat.card_targets('auto', ids) == (['yn-ct', 'yn-cm'], '三网')
    assert lat.card_targets('auto', ['cloudflare', 'openai']) == (['cloudflare', 'openai'], '国外')
    assert lat.card_targets('other', ['yn-ct', 'ali-dns']) == (['ali-dns'], '国内')
    assert lat.card_targets('other', ['yn-ct', 'ali-dns', 'github', 'c:a.example:1']) == (['ali-dns', 'github', 'c:a.example:1'], '其他地址')
    assert lat.card_targets('three', ['cloudflare']) == (['cloudflare'], '全部')
    assert lat.card_targets('all', ids) == (ids, '全部')
    assert lat.card_targets('auto', ['exit-1', 'exit-2']) == (['exit-1', 'exit-2'], '出口')


def test_a_servers_own_targets():
    assert lat.clean_targets(None) is None
    assert lat.clean_targets({'targets': ['bj-ct', 'ali-dns', 'c:203.0.113.9:443', 'c:home.example.com:22']})['targets'][2] == 'c:203.0.113.9:443'
    assert lat.clean_targets({'targets': []}) == {'targets': []}
    for bad in ({'targets': ['zz-ct']}, {'targets': ['bj-xx']}, {'targets': ['c:127.0.0.1:80']}, {'targets': ['c:localhost:80']},
                {'targets': ['c:host']}, {'targets': ['c:host:0']}, {'targets': ['c:bad host:80']}, {'targets': ['bj-ct', 'bj-ct']},
                {'targets': [f'c:h{i}.example.com:80' for i in range(17)]}, {'targets': 'bj-ct'}, {'other': []}):
        with pytest.raises(InstanceError):
            lat.clean_targets(bad)


def test_the_directory_and_labels():
    built = lat.builtin_directory()
    assert len(built) == 31 and built['yn'] == {'ct': 'yn-ct-v4.ip.zstaticcdn.com:80', 'cu': 'yn-cu-v4.ip.zstaticcdn.com:80', 'cm': 'yn-cm-v4.ip.zstaticcdn.com:80'}
    fresh = lat.parse_directory({'ProvinceNodes': [{'province': '云南', 'carriers': {'telecom': 'kmct.example.net:8080', 'unicom': 'bad value', 'mobile': 'x:99999'}},
                                                   {'province': '火星', 'carriers': {'telecom': 'mars.example:80'}}, 'junk']})
    assert fresh['yn'] == {**built['yn'], 'ct': 'kmct.example.net:8080'} and fresh['bj'] == built['bj'] and len(fresh) == 31
    assert [lat.label(t) for t in ('yn-ct', 'bj-cm', 'ali-dns', 'c:home.example.com:22', 'exit-1', 'exit-2')] == ['云南电信', '北京移动', '阿里 DNS', 'home.example.com', '出口', '备用出口']
    view = lat.catalog()
    assert len(view['provinces']) == 31 and [c['name'] for c in view['carriers']] == ['电信', '联通', '移动'] and {e['group'] for e in view['extras']} == {'cn', 'intl'}


def test_what_each_server_measures():
    settings, directory = lat.DEFAULTS, lat.builtin_directory()
    china = lat.targets_for(settings, {}, True, directory, 52002)
    assert [t['id'] for t in china] == ['yn-ct', 'yn-cu', 'yn-cm', 'ali-dns', 'tencent-dns']
    assert china[0] == {'id': 'yn-ct', 'host': 'yn-ct-v4.ip.zstaticcdn.com', 'port': 80, 'mode': 'tcp'}
    assert china[3] == {'id': 'ali-dns', 'host': '223.5.5.5', 'port': 53, 'mode': 'tcp'}
    abroad = lat.targets_for(settings, {}, False, directory, 52002)
    assert [t['id'] for t in abroad] == ['yn-ct', 'yn-cu', 'yn-cm', 'cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram']
    assert abroad[5] == {'id': 'openai', 'host': 'api.openai.com', 'port': 443, 'mode': 'tcp'}
    own = lat.targets_for(settings, {'latency': {'targets': ['gd-cu', 'c:home.example.com:22']}}, True, directory, 52002)
    assert own == [{'id': 'gd-cu', 'host': 'gd-cu-v4.ip.zstaticcdn.com', 'port': 80, 'mode': 'tcp'}, {'id': 'c:home.example.com:22', 'host': 'home.example.com', 'port': 22, 'mode': 'tcp'}]
    assert lat.targets_for(settings, {'latency': {'targets': []}}, True, directory, 52002) == []
    # A front reaches its exits only, on the port they serve it on, whatever else is set.
    front = {'front': {'exits': [{'serverId': 'a', 'address': '10.0.10.246'}, {'serverId': 'b', 'address': '198.51.100.227'}]}, 'latency': {'targets': ['bj-ct']}}
    assert lat.targets_for(settings, front, True, directory, 52002) == [{'id': 'exit-1', 'host': '10.0.10.246', 'port': 52002, 'mode': 'tcp'},
                                                                        {'id': 'exit-2', 'host': '198.51.100.227', 'port': 52002, 'mode': 'tcp'}]
    icmp = lat.targets_for({**settings, 'method': 'icmp', 'province': '北京'}, {}, False, directory, 52002)
    assert icmp[0] == {'id': 'bj-ct', 'host': 'bj-ct-v4.ip.zstaticcdn.com', 'port': 80, 'mode': 'icmp'}


def route(**tiers):
    """A 三网回程 result: tiers as ct='gold' or bj_cu='plain', for every city or one."""
    results = {}
    for key, tier in tiers.items():
        cities, _, carrier = key.rpartition('_') if '_' in key else ('', '', key)
        for city in [cities] if cities else ['bj', 'sh', 'gd']:
            results[f'{city}-{carrier}'] = {'line': {'gold': 'CN2 GIA', 'silver': 'CN2 GT', 'plain': '163', '': ''}[tier], 'tier': tier}
    return {'results': results}


def test_each_kind_of_server_follows_its_default_list():
    settings, directory = {**lat.DEFAULTS, 'defaults': {'domestic': ['three', 'ali-dns'], 'optimized': ['three', 'cloudflare'], 'plain': ['cloudflare', 'openai']}}, lat.builtin_directory()
    ids = lambda r, domestic=False, profile=None, s=settings: [t['id'] for t in lat.targets_for(s, profile or {}, domestic, directory, 52002, r)]  # noqa: E731
    # In China: the domestic list, all three carriers whatever its return.
    assert ids(route(ct='plain', cu='plain', cm='plain'), domestic=True) == ['yn-ct', 'yn-cu', 'yn-cm', 'ali-dns']
    # Abroad, not checked or nothing made out, or optimised: the optimised list.
    assert ids(None) == ids(route(ct='', cu='', cm='')) == ids(route(ct='gold', cu='gold', cm='silver')) == ['yn-ct', 'yn-cu', 'yn-cm', 'cloudflare']
    # Some carriers ordinary: the optimised list without them (a premium line to one city makes the carrier optimised).
    assert ids(route(ct='plain', bj_ct='gold', cu='plain', cm='silver')) == ['yn-ct', 'yn-cm', 'cloudflare']
    assert ids(route(bj_cm='plain', sh_cm='', gd_cm='')) == ['yn-ct', 'yn-cu', 'cloudflare']
    # All three ordinary: the plain list; there 'three' means all three, as asked for.
    assert ids(route(ct='plain', cu='plain', cm='plain')) == ['cloudflare', 'openai']
    plain_three = {**settings, 'defaults': {**settings['defaults'], 'plain': ['three', 'github']}}
    assert ids(route(ct='plain', cu='plain', cm='plain'), s=plain_three) == ['yn-ct', 'yn-cu', 'yn-cm', 'github']
    # A server's own list: as it is.
    assert ids(route(ct='plain'), profile={'latency': {'targets': ['yn-ct']}}) == ['yn-ct']
    assert lat.plain_carriers({'results': {'bj-ct': {'tier': 'plain'}, 'sh-ct': {'tier': 'silver'}}}) == set()
    assert lat.server_group(False, route(ct='plain', cu='plain', cm='plain')) == ('plain', set())
    # A return over CMI checked when it still counted as optimised: ordinary, as read today.
    old = {'results': {f'{city}-{c}': {'line': line, 'tier': tier} for city in ('bj', 'sh', 'gd')
                       for c, line, tier in (('ct', '163', 'plain'), ('cu', '4837', 'plain'), ('cm', 'CMI', 'silver'))}}
    assert lat.server_group(False, old) == ('optimized', {'ct', 'cu'})
    assert lat.server_group(False, retier(old)) == ('plain', set())


def test_other_servers_as_targets():
    # The public address the record has (IPv4 first), each its own way; none, the server itself or one gone: passed over.
    servers = {'ali': {'ipv4': '47.100.1.2'}, 'sg': {'ipv4': '103.1.2.3', 'ipv6': '2402:db8::1'}, 'v6': {'ipv6': '2a01:db8::5'}, 'home': {'ipv4': ''}, 'me': {'ipv4': '198.51.100.1'}}
    ids = ['s:ali', 's:sg:tcp:22', 's:sg:http:443', 's:v6:http:8080', 's:home', 's:gone', 's:me', 'cloudflare']
    found = lat.targets_for({**lat.DEFAULTS, 'method': 'icmp'}, {'latency': {'targets': ids}}, False, lat.builtin_directory(), 52002, servers=servers, self_id='me')
    assert found == [{'id': 's:ali', 'host': '47.100.1.2', 'mode': 'icmp'}, {'id': 's:sg:tcp:22', 'host': '103.1.2.3', 'mode': 'tcp', 'port': 22},
                     {'id': 's:sg:http:443', 'host': '103.1.2.3', 'mode': 'http', 'port': 443, 'url': 'https://103.1.2.3/'},
                     {'id': 's:v6:http:8080', 'host': '2a01:db8::5', 'mode': 'http', 'port': 8080, 'url': 'http://[2a01:db8::5]:8080/'},
                     {'id': 'cloudflare', 'host': '1.1.1.1', 'port': 443, 'mode': 'icmp'}]
    names = {'ali': '阿里云', 'sg': '新加坡节点'}
    assert [lat.label(t, names=names) for t in ('s:ali', 's:sg:tcp:22', 's:sg:http:443', 's:gone')] == ['阿里云', '新加坡节点 · TCP 22', '新加坡节点 · HTTP 443', '已删除的服务器']
    assert lat.clean_targets({'targets': ['s:ali', 's:sg:tcp:22', 's:local:http:80']}) == {'targets': ['s:ali', 's:sg:tcp:22', 's:local:http:80']}
    for bad in ('s:ali:tcp:0', 's:ali:tcp:70000', 's:ali:udp:53', 's:ali:tcp', 's:', 's:a b', 's:ali:icmp:1'):
        with pytest.raises(InstanceError):
            lat.clean_targets({'targets': [bad]})


def test_another_server_at_the_address_picked():
    # Its public IPv6, or its mesh address, which only a server on the mesh itself is sent.
    servers = {'sg': {'ipv4': '103.1.2.3', 'ipv6': '2402:db8::1', 'meshAddress': '100.96.0.3'}, 'ali': {'ipv4': '47.100.1.2', 'meshAddress': ''},
               'me': {'ipv4': '198.51.100.1', 'meshAddress': '100.96.0.1'}, 'off': {'ipv4': '198.51.100.2', 'meshAddress': ''}}
    ids = ['s:sg@v6', 's:sg@mesh:tcp:22', 's:sg@mesh:http:8080', 's:ali@v6', 's:ali@mesh']
    probe = lambda me: lat.targets_for({**lat.DEFAULTS, 'method': 'icmp'}, {'latency': {'targets': ids}}, False, lat.builtin_directory(), 52002, servers=servers, self_id=me)  # noqa: E731
    assert probe('me') == [{'id': 's:sg@v6', 'host': '2402:db8::1', 'mode': 'icmp'}, {'id': 's:sg@mesh:tcp:22', 'host': '100.96.0.3', 'mode': 'tcp', 'port': 22},
                           {'id': 's:sg@mesh:http:8080', 'host': '100.96.0.3', 'mode': 'http', 'port': 8080, 'url': 'http://100.96.0.3:8080/'}]
    assert probe('off') == [{'id': 's:sg@v6', 'host': '2402:db8::1', 'mode': 'icmp'}]
    assert lat.server_target('s:sg@mesh:tcp:22') == ('sg', 'tcp', 22, 'mesh') and lat.server_target('s:sg') == ('sg', 'icmp', 0, '')
    names = {'sg': '新加坡节点'}
    assert [lat.label(t, names=names) for t in ('s:sg@v6', 's:sg@mesh:tcp:22')] == ['新加坡节点 · IPv6', '新加坡节点 · 组网 · TCP 22']
    assert lat.clean_targets({'targets': ['s:sg@v6', 's:sg@mesh:tcp:22']}) == {'targets': ['s:sg@v6', 's:sg@mesh:tcp:22']}
    for bad in ('s:sg@v4', 's:sg@lan', 's:sg@', 's:sg:tcp:22@mesh'):
        with pytest.raises(InstanceError):
            lat.clean_targets({'targets': [bad]})


def test_the_users_own_addresses_each_their_way():
    value = lat.clean_settings({'custom': [{'label': 'NAS', 'host': 'NAS.example.com', 'group': 'cn', 'mode': 'icmp'},
                                           {'label': '博客', 'mode': 'http', 'url': ' https://Blog.Example.com/health?x=1 ', 'group': 'intl'},
                                           {'label': '面板', 'mode': 'http', 'url': 'http://[2001:db8::7]:8080/', 'group': 'intl'},
                                           {'label': '旧的', 'host': 'a.example', 'port': 22, 'group': 'cn'}]})
    nas, blog, panel, old = value['custom']
    assert {k: v for k, v in nas.items() if k != 'id'} == {'label': 'NAS', 'host': 'nas.example.com', 'port': 0, 'group': 'cn', 'mode': 'icmp'}
    assert {k: v for k, v in blog.items() if k != 'id'} == {'label': '博客', 'host': 'blog.example.com', 'port': 443, 'group': 'intl', 'mode': 'http',
                                                            'url': 'https://Blog.Example.com/health?x=1'}
    assert (panel['host'], panel['port']) == ('2001:db8::7', 8080) and 'mode' not in old
    # Saved again as they are.
    assert lat.clean_settings({'custom': value['custom']}, value)['custom'] == value['custom']
    ids = [nas['id'], blog['id'], old['id']]
    tcp = lat.targets_for(value, {'latency': {'targets': ids}}, False, lat.builtin_directory(), 52002)
    assert tcp == [{'id': nas['id'], 'host': 'nas.example.com', 'mode': 'icmp'}, {'id': blog['id'], 'host': 'blog.example.com', 'port': 443, 'mode': 'http',
                                                                                  'url': 'https://Blog.Example.com/health?x=1'},
                   {'id': old['id'], 'host': 'a.example', 'port': 22, 'mode': 'tcp'}]
    # One without a way of its own follows the settings.
    icmp = lat.targets_for({**value, 'method': 'icmp'}, {'latency': {'targets': ids}}, False, lat.builtin_directory(), 52002)
    assert [t['mode'] for t in icmp] == ['icmp', 'http', 'icmp']
    for bad in ({'label': 'x', 'mode': 'http', 'group': 'cn'}, {'label': 'x', 'mode': 'http', 'url': 'ftp://a.example/', 'group': 'cn'},
                {'label': 'x', 'mode': 'http', 'url': 'https://user:pw@a.example/', 'group': 'cn'}, {'label': 'x', 'mode': 'http', 'url': 'http://localhost/', 'group': 'cn'},
                {'label': 'x', 'mode': 'http', 'url': 'http://a.example:99999/', 'group': 'cn'}, {'label': 'x', 'mode': 'tcp', 'host': 'a.example', 'group': 'cn'},
                {'label': 'x', 'mode': 'udp', 'host': 'a.example', 'port': 1, 'group': 'cn'}, {'label': 'x', 'mode': 'icmp', 'host': '127.0.0.1', 'group': 'cn'}):
        with pytest.raises(InstanceError) as raised:
            lat.clean_settings({'custom': [bad]})
        assert raised.value.error == 'invalid_latency_custom'


def test_what_a_card_shows_when_picked():
    ids = ['yn-ct', 'yn-cm', 'cloudflare', 'openai']
    assert lat.card_targets('auto', ids, pins=['openai']) == (['openai'], 'OpenAI')
    assert lat.card_targets('auto', ids, pins=['openai', 'yn-cm', 'gone']) == (['yn-cm', 'openai'], '所选 2 个')
    # None of those picked measured now: the settings' rule.
    assert lat.card_targets('auto', ids, pins=['gone']) == (['yn-ct', 'yn-cm'], '三网')
    assert lat.card_targets('auto', ['s:ali', 'yn-ct'], pins=['s:ali'], names={'ali': '阿里云'}) == (['s:ali'], '阿里云')
    # Picks on their own keep the default list; neither is following everything.
    assert lat.clean_targets({'card': ['yn-ct']}) == {'card': ['yn-ct']} and lat.own_ids({'latency': {'card': ['yn-ct']}}) is None
    assert lat.clean_targets({'targets': ['yn-ct'], 'card': []}) == {'targets': ['yn-ct']}
    assert lat.clean_targets({'targets': None, 'card': None}) is None
    for bad in ({}, {'card': ['nope']}, {'card': 'yn-ct'}, {'card': ['yn-ct', 'yn-ct']}):
        with pytest.raises(InstanceError):
            lat.clean_targets(bad)
