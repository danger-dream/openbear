"""DNS record names in 动态域名 reminders are quoted like every other reminder name: Telegram shows
them as code, not as links (tests/test_server_notice_names.py covers the other modules)."""
from tests.ddns import fakeprovider
from tests.ddns.harness import (  # noqa: F401
    add_account,
    add_node,
    advance,
    alerts,
    base_env,
    env,
    record,
    sync,
    url_record,
)
from tests.test_server_notice_names import assert_names_safe


async def test_failure_drift_and_recovery_reminders_quote_the_record_name(env):  # noqa: F811
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account, name='paypal.example.com'))['records'][0]['id']
    api = fakeprovider.store('tok')
    pid = (await record(e, rid))['providerRecordId']
    api.records[pid].value = '192.0.2.10'
    await sync(e, rid)
    api.fail('auth_failed')
    advance(e, 3600)
    await sync(e, rid)
    advance(e, 3600)
    await e.ddns.kick(rid, 'manual')
    await e.ddns.idle()
    sent = await alerts(e)
    assert [a['kind'] for a in sent] == ['ddns_drift', 'ddns_fail', 'ddns_recover'], sent
    for alert in sent:
        assert alert['text'].startswith('「paypal.example.com」（A）'), alert['text']
        assert_names_safe(alert['text'], 'paypal.example.com')
