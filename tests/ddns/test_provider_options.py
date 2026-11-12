import pytest

from app.ddns.model import DdnsError
from app.ddns.providers.base import Field, REGISTRY
from tests.ddns import fakeprovider
from tests.ddns.harness import OWNER, add_account, add_node, base_env, env, probe_result  # noqa: F401


async def test_mixed_providers_preview_and_save_each_line_separately(env, monkeypatch):
    class Other(fakeprovider.FakeProvider):
        type = 'otherline'
        options = (Field('line', '线路', 'text', optional=True),)

    monkeypatch.setitem(REGISTRY, Other.type, Other)
    sid = await add_node(env)
    env.nodes.connect(sid)
    env.nodes.probe[sid] = probe_result(url='198.51.100.7')
    first = await add_account(env)
    fakeprovider.store('other').zones = ['example.net']
    second = await add_account(env, token='other', kind=Other.type)
    fakeprovider.store('tok').add('home.example.com', 'A', '198.51.100.7', line='telecom')
    fakeprovider.store('tok').add('home.example.com', 'A', '198.51.100.99', line='default')
    fakeprovider.store('other').add('home.example.net', 'A', '198.51.100.7', line='ln-123')
    fakeprovider.store('other').add('home.example.net', 'A', '198.51.100.99', line='default')
    options = {'fake': {'line': 'telecom'}, Other.type: {'line': 'ln-123'}}
    preview = await env.ddns.preview(OWNER, {'serverId': sid, 'source': {'method': 'url'},
        'names': ['home.example.com', 'home.example.net'], 'types': ['A'], 'optionsByProvider': options})
    assert [row['action'] for row in preview['rows']] == ['same', 'same']
    assert [row['remoteCount'] for row in preview['rows']] == [1, 1]
    result = await env.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'},
        'options': {'line': 'default'}, 'rows': [
            {'name': 'home.example.com', 'type': 'A', 'accountId': first['id'], 'options': options['fake']},
            {'name': 'home.example.net', 'type': 'A', 'accountId': second['id'], 'options': options[Other.type]},
        ]})
    await env.ddns.idle()
    assert result['skipped'] == []
    assert [record['options'] for record in result['records']] == [{'line': 'telecom'}, {'line': 'ln-123'}]
    assert (await env.ddns.overview(OWNER))['features']['providerOptions'] is True


@pytest.mark.parametrize('options', [None, [], {'fake': 'telecom'}])
async def test_provider_options_reject_invalid_mapping(env, options):
    with pytest.raises(DdnsError) as exc:
        await env.ddns.preview(OWNER, {'names': ['home.example.com'], 'optionsByProvider': options})
    assert exc.value.error == 'invalid_parameters'
