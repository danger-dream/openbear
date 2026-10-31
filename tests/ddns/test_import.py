"""Importing a ddns-go config: parsing alone, then preview and apply against the fake provider
registered under the ddns-go provider names the sample uses."""
import pytest

from app.ddns import importer
from app.ddns.model import DdnsError
from app.ddns.providers.base import REGISTRY, Field
from tests.ddns import fakeprovider
from tests.ddns.harness import OWNER, add_node, base_env, env, probe_result  # noqa: F401

SAMPLE = '''dnsconf:
    - name: ""
      ipv4:
        enable: true
        gettype: url
        url: https://api4.ipify.org, https://ip.3322.net, https://ddns.oray.com/checkip
        netinterface: eth0
        cmd: ""
        domains:
            - hinet:example.com
            - ""
      ipv6:
        enable: false
        gettype: netInterface
        url: https://speed.neu6.edu.cn/getIP.php, https://v6.ident.me, https://6.ipw.cn, https://v6.yinghualuo.cn/bejson
        netinterface: ""
        cmd: ""
        ipv6reg: ""
        domains:
            - ""
      dns:
        name: cloudflare
        id: ""
        secret: fake-cloudflare-token-0000
      ttl: "1"
      httpinterface: ""
user:
    username: admin
    password: $2a$10$fakehash
webhook:
    webhookurl: ""
notallowwanaccess: true
lang: zh
'''

MORE = '''dnsconf:
    - name: 家里
      ipv4:
        enable: true
        gettype: cmd
        cmd: curl -s ip.sb
        domains:
            - a.example.com?proxied=true&RecordId=rec-9
            - b.example.com?line=电信&foo=bar
      ipv6:
        enable: true
        gettype: netInterface
        netinterface: eth1
        ipv6reg: "@2"
        domains:
            - a.example.com
      dns:
        name: cloudflare
        secret: other-token
      ttl: "600"
    - name: 旧
      ipv4: {enable: true, gettype: url, url: "", domains: [x.example.net]}
      dns: {name: edgeone, id: a, secret: b}
    - ipv4: {enable: true, domains: [y.example.org]}
      dns: {name: namecheap, id: a, secret: b}
webhook:
    webhookurl: https://hook.test/x
'''


class Cloudflare(fakeprovider.FakeProvider):
    type = 'cloudflare'
    name = 'Cloudflare'
    fields = (Field('token', 'API 令牌', 'password'),)


@pytest.fixture
def cloudflare(monkeypatch):
    fakeprovider.reset()
    monkeypatch.setitem(REGISTRY, 'cloudflare', Cloudflare)
    monkeypatch.delitem(REGISTRY, 'namecheap', raising=False)


def test_parse_sample(cloudflare):
    parsed = importer.parse(SAMPLE)
    assert parsed['accounts'] == [{'key': 0, 'type': 'cloudflare', 'alias': 'Cloudflare', 'credentials': {'token': 'fake-cloudflare-token-0000'},
                                   'ttl': None}]
    [rec] = parsed['records']
    assert rec['name'] == 'hinet.example.com' and rec['zone'] == 'example.com' and rec['type'] == 'A' and rec['method'] == 'url'
    assert rec['params']['urls'] == ['https://api4.ipify.org', 'https://ip.3322.net', 'https://ddns.oray.com/checkip']
    assert rec['options'] == {} and rec['recordId'] == '' and parsed['unsupported'] == [] and parsed['warnings'] == []


def test_parse_parameters_methods_and_unsupported(cloudflare):
    parsed = importer.parse(MORE)
    assert [a['alias'] for a in parsed['accounts']] == ['家里'] and parsed['accounts'][0]['ttl'] == 600
    recs = {(r['name'], r['type']): r for r in parsed['records']}
    a = recs[('a.example.com', 'A')]
    assert a['method'] == 'cmd' and a['params']['cmd'] == 'curl -s ip.sb' and a['options'] == {'proxied': True} and a['recordId'] == 'rec-9'
    assert recs[('b.example.com', 'A')]['options'] == {'line': 'telecom'}
    v6 = recs[('a.example.com', 'AAAA')]
    assert v6['method'] == 'nic' and v6['params']['iface'] == 'eth1' and v6['params']['v6Select']['mode'] == 'index' \
        and v6['params']['v6Select']['value'] == '2'
    assert {u['reason'] for u in parsed['unsupported']} == {'暂不支持这个服务商', 'OpenBear 还没有这个服务商的适配'}
    assert any('foo=bar' in w for w in parsed['warnings']) and any('Webhook' in w for w in parsed['warnings'])
    with pytest.raises(DdnsError) as err:
        importer.parse('just: [text')
    assert err.value.error == 'invalid_yaml'


def test_parse_refuses_alias_bombs_and_too_many_domains(cloudflare):
    head = 'dnsconf:\n  - dns: {name: cloudflare, secret: t}\n    ipv4:\n      enable: true\n      domains: '
    # Nine levels of ten aliases: a few hundred bytes naming a billion domains.
    bomb = 'a0: &a0 [x.example.com]\n' + ''.join(f'a{n}: &a{n} [{", ".join([f"*a{n - 1}"] * 10)}]\n' for n in range(1, 10))
    with pytest.raises(DdnsError) as err:
        importer.parse(bomb + head + '*a9\n')
    assert err.value.error == 'import_too_large' and 'YAML 别名' in err.value.message
    many = head + '[' + ', '.join(f'h{n}.example.com' for n in range(importer.MAX_DOMAINS + 1)) + ']\n'
    with pytest.raises(DdnsError) as err:
        importer.parse(many)
    assert err.value.error == 'import_too_large' and str(importer.MAX_DOMAINS) in err.value.message
    # At the limits it is read as before.
    few = 'shared: &d [a.example.com, b.example.com]\n' + head + '*d\n'
    assert [r['name'] for r in importer.parse(few)['records']] == ['a.example.com', 'b.example.com']
    most = head + '[' + ', '.join(f'h{n}.example.com' for n in range(importer.MAX_DOMAINS)) + ']\n'
    assert len(importer.parse(most)['records']) == importer.MAX_DOMAINS


async def test_import_preview_then_apply_then_merge(env, cloudflare):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url='198.51.100.7')
    api = fakeprovider.store('fake-cloudflare-token-0000')
    api.add('hinet.example.com', 'A', '192.0.2.44')
    preview = await e.ddns.import_preview(OWNER, {'yaml': SAMPLE, 'serverId': sid})
    assert preview['accounts'] == [{'type': 'cloudflare', 'alias': 'Cloudflare', 'mergeInto': None}]
    assert preview['records'] == [{'name': 'hinet.example.com', 'type': 'A', 'method': 'url', 'action': 'takeover', 'remote': '192.0.2.44',
                                   'value': '198.51.100.7'}]
    assert preview['unsupported'] == [] and preview['warnings'] == []
    assert 'fake-cloudflare-token-0000' not in str(preview)
    # Not confirmed: the account comes in, the record waits for the takeover.
    applied = await e.ddns.import_apply(OWNER, {'yaml': SAMPLE, 'serverId': sid})
    await e.ddns.idle()
    assert [a['type'] for a in applied['accounts']] == ['cloudflare'] and applied['records'] == []
    assert applied['skipped'] == [{'name': 'hinet.example.com', 'type': 'A', 'reason': 'takeover_not_confirmed', 'remote': '192.0.2.44'}]
    account = applied['accounts'][0]
    assert account['secret']['name'] == 'ddns/Cloudflare-Cloudflare' and account['zones'] == ['example.com']
    # The same credentials again merge into that account.
    preview = await e.ddns.import_preview(OWNER, {'yaml': SAMPLE, 'serverId': sid})
    assert preview['accounts'][0]['mergeInto'] == account['id']
    applied = await e.ddns.import_apply(OWNER, {'yaml': SAMPLE, 'serverId': sid, 'confirmTakeover': True})
    await e.ddns.idle()
    assert [a['id'] for a in applied['accounts']] == [account['id']] and len((await e.ddns.overview(OWNER))['accounts']) == 1
    [rec] = applied['records']
    assert rec['source']['method'] == 'url' and rec['source']['params']['urls'][0] == 'https://api4.ipify.org'
    assert api.values('hinet.example.com') == ['198.51.100.7']
    preview = await e.ddns.import_preview(OWNER, {'yaml': SAMPLE, 'serverId': sid})
    assert preview['records'][0]['action'] == 'exists'


def test_rainyun_and_dynadot_get_the_credentials_their_apis_take():
    """From the 10-30 check: rainyun's domain id went in as its API key and the key itself was
    dropped; dynadot's dynamic DNS password went in as an API Key it is not."""
    out = importer.parse('''
dnsconf:
  - name: ry
    dns: {name: rainyun, id: "123456", secret: "RY-API-KEY"}
    ipv4: {enable: true, gettype: url, url: "https://api4.ipify.org", domains: ["www.example.com"]}
  - name: dd
    dns: {name: dynadot, id: "", secret: "ddns-password"}
    ipv4: {enable: true, gettype: url, url: "https://api4.ipify.org", domains: ["home.example.net"]}
''')
    by = {a['type']: a for a in out['accounts']}
    assert by['rainyun']['credentials'] == {'key': 'RY-API-KEY'} and by['dynadot']['credentials'] == {}
    assert any('API Key' in w and w.startswith('dd：') for w in out['warnings'])
    assert not any(w.startswith('ry：凭据按顺序') for w in out['warnings'])
