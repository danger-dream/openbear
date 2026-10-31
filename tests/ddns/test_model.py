import pytest

from app.ddns import model, nodecheck
from app.ddns.model import DdnsError
from tests.ddns.fakeprovider import FakeProvider


def error_of(fn, *args, **kwargs):
    with pytest.raises(DdnsError) as err:
        fn(*args, **kwargs)
    return err.value.error


@pytest.mark.parametrize('value,family,allow,ok,reason', [
    ('198.51.100.7', 'v4', False, '198.51.100.7', ''),
    ('10.1.2.3', 'v4', False, '', 'private'),
    ('192.168.1.1', 'v4', False, '', 'private'),
    ('100.64.0.1', 'v4', False, '', 'private'),        # CGNAT
    ('169.254.1.1', 'v4', False, '', 'private'),       # link-local
    ('224.0.0.1', 'v4', False, '', 'reserved'),        # multicast
    ('240.0.0.1', 'v4', False, '', 'reserved'),
    ('0.0.0.0', 'v4', True, '', 'reserved'),
    ('10.1.2.3', 'v4', True, '10.1.2.3', ''),           # allowPrivate
    ('100.64.0.1', 'v4', True, '100.64.0.1', ''),
    ('2001:db8::1', 'v4', False, '', 'family'),
    ('::ffff:198.51.100.7', 'v6', False, '', 'family'),  # v4-mapped
    ('2400:cb00::1', 'v6', False, '2400:cb00::1', ''),
    ('fd00::1', 'v6', False, '', 'private'),            # ULA
    ('fe80::1', 'v6', False, '', 'private'),
    ('ff02::1', 'v6', False, '', 'reserved'),
    ('fd00::1', 'v6', True, 'fd00::1', ''),
    ('not-an-ip', 'v4', False, '', 'invalid'),
])
def test_address_filter(value, family, allow, ok, reason):
    assert model.check_address(value, family, allow) == (ok, reason)


def test_names():
    assert model.clean_name('Home.Example.COM.') == ('home.example.com', '')
    assert model.clean_name('hinet:example.com') == ('hinet.example.com', 'example.com')
    assert model.clean_name('@:example.com') == ('example.com', 'example.com')
    assert model.clean_name('家.例子.中国') == ('xn--fct.xn--fsqu00a.xn--fiqs8s', '')
    for bad in ('', 'localhost', 'a..b.com', '-a.example.com', 'a.*.example.com', 'x' * 64 + '.com', 42):
        assert error_of(model.clean_name, bad) == 'invalid_name'


def test_source_validation():
    src = model.clean_source({'serverId': 's1'})
    assert src['method'] == 'auto' and src['params']['v6Select'] == model.DEFAULT_V6_SELECT
    assert error_of(model.clean_source, {'method': 'url'}) == 'server_required'
    assert error_of(model.clean_source, {'serverId': 's1', 'method': 'magic'}) == 'invalid_method'
    assert error_of(model.clean_source, {'serverId': 's1', 'method': 'cmd'}) == 'cmd_required'
    assert error_of(model.clean_source, {'serverId': 's1', 'method': 'url', 'params': {'urls': ['ftp://x']}}) == 'invalid_urls'
    assert error_of(model.clean_source, {'serverId': 's1', 'method': 'url', 'params': {'urls': ['https://a.test'] * 9}}) == 'invalid_urls'
    longest = 'https://a.test/' + 'p' * (model.MAX_URL - len('https://a.test/'))
    assert model.clean_source({'serverId': 's1', 'method': 'url', 'params': {'urls': [longest]}})['params']['urls'] == [longest]
    assert error_of(model.clean_source, {'serverId': 's1', 'method': 'url', 'params': {'urls': [longest + 'p']}}) == 'invalid_urls'
    stun = ['stun:stun.l.google.com:19302', 'stun.cloudflare.com', '[2001:db8::1]:3478', 'stun:198.51.100.3']
    assert model.clean_source({'serverId': 's1', 'method': 'stun', 'params': {'stun': stun}})['params']['stun'] == stun
    for bad in (['host:99999'], ['a b'], ['x'] * 5, ['stun:']):
        assert error_of(model.clean_source, {'serverId': 's1', 'method': 'stun', 'params': {'stun': bad}}) == 'invalid_stun'
    assert model.clean_source({'serverId': 's1', 'params': {'v6Select': {'mode': 'index', 'value': '1'}}})['params']['v6Select']['value'] == '1'
    assert error_of(model.clean_source, {'serverId': 's1', 'params': {'v6Select': {'mode': 'index', 'value': '0'}}}) == 'invalid_v6_select'
    assert error_of(model.clean_source, {'serverId': 's1', 'method': 'nic', 'params': {'intervalS': 5}}) == 'invalid_interval'
    assert model.clean_source({'serverId': 's1', 'method': 'nic', 'params': {'intervalS': 10}})['params']['intervalS'] == 10
    assert error_of(model.clean_source, {'serverId': 's1', 'method': 'url', 'params': {'intervalS': 10}}) == 'invalid_interval'
    assert error_of(model.clean_source, {'serverId': 's1', 'params': {'v6Select': {'mode': 'regex', 'value': '('}}}) == 'invalid_v6_select'
    assert error_of(model.clean_source, {'serverId': 's1', 'params': {'cmd': 'x' * 1001}}) == 'invalid_cmd'
    fixed = model.clean_source({'kind': 'fixed', 'fixed': '198.51.100.1'})
    assert fixed['serverId'] == '' and fixed['fixed'] == '198.51.100.1'
    assert error_of(model.check_fixed, fixed, 'v6') == 'invalid_fixed'
    assert model.clean_source({'serverId': 's1', 'method': 'url', 'resolvedMethod': 'stun'})['resolvedMethod'] == ''


# Each checked against Go 1.24's regexp.Compile and the node's ddnsValidURL (url.Parse): True where
# Go takes it. The controller may refuse more than Go (a \p{Script} class, brackets in a host that
# are not an IPv6 literal), never less.
@pytest.mark.parametrize('pattern,ok', [
    (r'^2001:db8:', True), (r'(?i)abc', True), (r'(?P<n>a)', True), (r'(?<n>a)', True), (r'[[:alpha:]]', True), (r'\pL', True),
    (r'\p{L}', True), (r'\Q*\E', True), (r'a*?', True), (r'\z', True), (r'(?U)a+', True), (r'\x{10FFFF}', True), (r'', True),
    (r'(?=x)', False), (r'(?<=a)b', False), (r'(a)\1', False), (r'a{1001}', False), (r'a{2,1}', False), (r'[[:foo:]]', False),
    (r'a**', False), (r'a++', False), (r'\Z', False), (r'\C', False), (r'[a-', False), (r'[z-a]', False), (r'x{2}{3}', False),
    (r'(a{500}){3}', False), (r'\x{110000}', False), (r'\8', False), (r'[\d-z]', True), (r'(', False), (r'a|', True),
])
def test_regex_as_the_node_compiles_it(pattern, ok):
    assert nodecheck.regex_ok(pattern) is ok


@pytest.mark.parametrize('url,ok', [
    ('https://api.ipify.org', True), ('http://[::1]:80/x', True), ('http://[fe80::1%25eth0]/', True), ('https://user:pw@host/p?q#f', True),
    ('HTTPS://Host', True), ('https://ho%41st', False), ('https://x/?q=%zz', True), ('https://例子.cn/', True),
    ('https://a b', False), ('https://', False), ('http:///x', False), ('https://host:99999', True), ('https://host:abc', False),
    ('https://ho%zzst', False), ('https://[::1', False), ('ftp://x', False), ('https://x/%zz', False), ('https://x#%zz', False),
    ('https://x:', True), ('https://:80', False), ('https://x/\x7f', False),
])
def test_urls_as_the_node_parses_them(url, ok):
    assert nodecheck.url_ok(url) is ok


def test_probe_parameters_the_node_would_refuse_are_refused():
    for value in (r'(?=2001)', r'(a)\1'):
        assert error_of(model.clean_source, {'serverId': 's1', 'params': {'v6Select': {'mode': 'regex', 'value': value}}}) == 'invalid_v6_select'
    for urls in (['https://x/%zz'], ['https://[::1'], [42]):
        assert error_of(model.clean_source, {'serverId': 's1', 'method': 'url', 'params': {'urls': urls}}) == 'invalid_urls'
    assert error_of(model.clean_source, {'serverId': 's1', 'params': {'v6Select': {'mode': 'prefix', 'value': ' '}}}) == 'invalid_v6_select'
    assert error_of(model.clean_source, {'serverId': 's1', 'method': 'stun', 'params': {'stun': ['[stun.test]:3478']}}) == 'invalid_stun'


def test_policy_backup_settings_options():
    assert model.clean_policy(None) == model.DEFAULT_POLICY
    assert model.clean_policy({'onDrift': 'alert'}, {'multi': 'all'}) == {'multi': 'all', 'allowPrivate': False, 'onDrift': 'alert'}
    assert error_of(model.clean_policy, {'multi': 'many'}) == 'invalid_policy'
    assert model.clean_backup({'serverId': 'b'}, 'a') == {'serverId': 'b', 'afterS': 300}
    assert error_of(model.clean_backup, {'serverId': 'a'}, 'a') == 'invalid_backup'
    assert error_of(model.clean_backup, {'serverId': 'b', 'afterS': 10}, 'a') == 'invalid_backup'
    assert model.clean_settings({'endpoint': 'https://api.test', 'ttl': 600}) == {'endpoint': 'https://api.test', 'proxy': '', 'ttl': 600}
    assert error_of(model.clean_settings, {'proxy': 'ftp://p'}) == 'invalid_proxy'
    # socks proxies are not taken any more (httpx needs an extra package for them).
    for proxy in ('socks5://127.0.0.1:1080', 'socks5h://p:1080'):
        assert error_of(model.clean_settings, {'proxy': proxy}) == 'invalid_proxy'
    assert model.clean_settings({'proxy': 'https://p:8443'})['proxy'] == 'https://p:8443'
    assert error_of(model.clean_settings, {'ttl': 0.5}) == 'invalid_ttl'
    assert model.clean_options(FakeProvider, {'proxied': True, 'line': '电信'}) == {'proxied': True, 'line': 'telecom'}
    assert error_of(model.clean_options, FakeProvider, {'line': 'mobile'}) == 'invalid_options'
    assert error_of(model.clean_options, FakeProvider, {'weight': 1}) == 'invalid_options'
    assert model.clean_options(FakeProvider, {'weight': 1}, strict=False) == {}


def test_credentials():
    assert model.clean_credentials(FakeProvider, {'token': 't'}) == {'token': 't'}
    assert error_of(model.clean_credentials, FakeProvider, {'token': ''}) == 'credential_missing'
    assert error_of(model.clean_credentials, FakeProvider, {'other': 'x'}) == 'invalid_credentials'
    assert model.clean_credentials(FakeProvider, {'token': ''}, partial=True) == {}
    assert model.clean_secret_ref(FakeProvider, {'secretId': 3}) == (3, {'token': 'token'})
    assert error_of(model.clean_secret_ref, FakeProvider, {'secretId': '3'}) == 'invalid_secret_ref'


def test_probe_keys_dedupe_by_method_parameters_and_family():
    a = model.clean_source({'serverId': 's', 'method': 'url'})
    b = model.clean_source({'serverId': 's', 'method': 'url', 'params': {'intervalS': 30}})
    c = model.clean_source({'serverId': 's', 'method': 'url', 'params': {'urls': ['https://ip.test']}})
    pa, pb, pc = (model.probe_spec(s, 'v4', False) for s in (a, b, c))
    assert pa['key'] == pb['key'] != pc['key'] and pa['key'].startswith('p_')
    assert model.probe_spec(a, 'v6', False)['key'] != pa['key']
    assert pa['urls'] == model.DEFAULT_URLS['v4'] and pa['confirm'] == 2 and pa['intervalS'] == 60
    merged = model.merge_probes([pa, pb, pc])
    assert len(merged) == 2 and next(p for p in merged if p['key'] == pa['key'])['intervalS'] == 30
    nic = model.clean_source({'serverId': 's', 'method': 'nic'})
    # allowPrivate reaches the node only for v6 nic probes (ULA); v4 is filtered here.
    assert model.probe_spec(nic, 'v6', True)['key'] != model.probe_spec(nic, 'v6', False)['key']
    assert model.probe_spec(nic, 'v4', True) == model.probe_spec(nic, 'v4', False)
    assert model.probe_spec(nic, 'v6', True)['allowPrivate'] is True and model.probe_spec(a, 'v4', True)['allowPrivate'] is False
    auto = model.clean_source({'serverId': 's'})
    assert model.probe_spec(auto, 'v6', False)['method'] == 'nic' and model.probe_spec(auto, 'v4', False)['method'] == 'url'


def test_recommend():
    iface = [{'name': 'eth0', 'v4': ['10.0.0.2']}]
    assert model.recommend('v4', [{'method': 'nic', 'value': '198.51.100.2'}], [])[0] == 'nic'
    assert model.recommend('v4', [{'method': 'url', 'value': '198.51.100.2'}, {'method': 'stun', 'value': '198.51.100.9'}], iface)[0] == 'stun'
    assert model.recommend('v4', [{'method': 'url', 'value': '198.51.100.2'}, {'method': 'stun', 'value': '198.51.100.2'}], iface)[0] == 'url'
    method, notes = model.recommend('v4', [{'method': 'url', 'value': '198.51.100.2'}], [{'name': 'eth0', 'v4': ['100.64.3.3']}])
    assert method == 'url' and 'CGNAT' in notes[0]['text']
    assert model.recommend('v6', [{'method': 'url', 'value': '2400:cb00::1'}], [])[0] == 'nic'


def test_recommend_behind_a_proxy_that_splits_china_and_abroad():
    # A side router sends traffic abroad (HTTP and UDP alike) through a VPS: services in China
    # see the line, the ones abroad and STUN see the VPS.
    line, vps = '192.0.2.34', '192.0.2.92'

    def results(cn=line, intl=vps, stun=vps):
        return [{'method': 'url_cn', 'value': cn}, {'method': 'url_intl', 'value': intl}, {'method': 'nic', 'value': ''}, {'method': 'stun', 'value': stun}]
    iface = [{'name': 'eth0', 'v4': ['10.20.20.7']}]
    method, notes = model.recommend('v4', results(), iface)
    assert method == 'url_cn' and line in notes[-1]['text'] and '不一致' in notes[0]['text']
    # The user says the server is abroad: the services there are believed instead.
    assert model.recommend('v4', results(), iface, region='SG')[0] == 'url_intl'
    assert model.recommend('v4', results(), iface, region='cn')[0] == 'url_cn'
    # What a mesh peer on a direct link sees settles it, whatever the region says.
    method, notes = model.recommend('v4', results(), iface, seen={line}, region='SG')
    assert method == 'url_cn' and '组网' in notes[-1]['text']
    assert model.recommend('v4', results(), iface, seen={vps})[0] == 'url_intl'
    # Only HTTP proxied: both groups agree, STUN differs.
    assert model.recommend('v4', results(cn=vps, intl=vps, stun=line), iface)[0] == 'stun'
    # Services abroad unreachable: the ones in China rather than STUN through the proxy.
    assert model.recommend('v4', results(intl=''), iface)[0] == 'url_cn'
    assert model.recommend('v4', results(intl='', stun=''), iface)[0] == 'url_cn'
    # No proxy: one answer, the interface method over every service.
    assert model.recommend('v4', results(cn=line, intl=line, stun=line), iface) == ('url', [])
    method, notes = model.recommend('v4', results(cn=vps, intl=vps, stun=vps), iface, seen={line})
    assert method == 'url' and line in notes[0]['text']


def test_probes_of_a_settled_group():
    source = model.clean_source({'serverId': 's1', 'method': 'auto', 'resolvedMethod': 'url_cn'})
    spec = model.probe_spec(source, 'v4', False)
    assert spec['method'] == 'url' and spec['urls'] == model.URL_GROUPS['cn']['v4'] and spec['intervalS'] == 60
    assert model.probe_spec({**source, 'resolvedMethod': 'url_intl'}, 'v4', False)['urls'] == model.URL_GROUPS['intl']['v4']
    assert model.probe_spec({**source, 'resolvedMethod': 'url'}, 'v4', False)['urls'] == model.DEFAULT_URLS['v4']
    assert spec['key'] != model.probe_spec({**source, 'resolvedMethod': 'url_intl'}, 'v4', False)['key']
    assert all(len(group[f]) <= model.MAX_URLS for group in model.URL_GROUPS.values() for f in ('v4', 'v6'))
    assert all(len(urls) <= model.MAX_URLS for urls in model.DEFAULT_URLS.values())
