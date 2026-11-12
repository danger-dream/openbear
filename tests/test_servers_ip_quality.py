"""IP 质量 on the detail page: an IP.Check.Place report summed up, what changed since the run before, and the
endpoint that reads the latest finished run and one under way."""
from __future__ import annotations

import asyncio
import copy
import json
import time
import unittest.mock
import zlib

from app.servers import ip_quality
from app.webhooks.repository import many
from tests.test_mesh_service import OWNER, server
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

REPORT = {
    'Head': {'IP': '203.0.*.*'},
    'Info': {'ASN': '198454', 'Organization': 'Sistec Telecom S.l.', 'Latitude': '36.8711', 'Longitude': '-4.7036', 'TimeZone': 'Europe/Madrid',
             'City': {'Name': '卡尔塔马', 'Subdivisions': 'Andalusia'}, 'Region': {'Code': 'es', 'Name': '西班牙'},
             'RegisteredRegion': {'Code': 'ES', 'Name': '西班牙'}, 'Type': '原生IP'},
    'Type': {'Usage': {'IPinfo': '家宽', 'ipapi': '家宽', 'AbuseIPDB': '机房'}, 'Company': {'IPinfo': '家宽', 'ipapi': '商业'}},
    'Score': {'IP2LOCATION': '0', 'SCAMALYTICS': '34', 'ipapi': '0.35%', 'AbuseIPDB': 75, 'IPQS': 'null', 'DBIP': ''},
    'Factor': {'Proxy': {'IP2LOCATION': False, 'IPQS': True, 'DBIP': None}, 'Tor': {'IPQS': False},
               'VPN': {'ipapi': True, 'IPinfo': True, 'IPQS': False}, 'Server': {}, 'Abuser': {'IPQS': True}, 'Robot': 'odd'},
    'Media': {'Netflix': {'Status': '解锁', 'Region': 'ES', 'Type': '原生'}, 'ChatGPT': {'Status': '屏蔽', 'Region': '', 'Type': ''}, 'Unknown': {'Status': 'x'}},
    'Mail': {'Port25': False, 'DNSBlacklist': {'Total': 423, 'Clean': 399, 'Marked': 24, 'Blacklisted': 1}},
}


def test_a_report_summed_up():
    got = ip_quality.summarize(REPORT)
    assert got['location'] == {'lat': 36.8711, 'lon': -4.7036, 'city': '卡尔塔马', 'subdivision': 'Andalusia', 'country': 'ES', 'countryName': '西班牙',
                               'registered': 'ES', 'registeredName': '西班牙', 'timezone': 'Europe/Madrid'}
    assert (got['asn'], got['org'], got['kind']) == ('198454', 'Sistec Telecom S.l.', '原生IP')
    # Each database's score on 0-100 and its level; none where it gave none.
    assert [(s['source'], s['value'], s['level']) for s in got['scores']] == [
        ('IP2LOCATION', 0.0, 'low'), ('SCAMALYTICS', 34.0, 'mid'), ('ipapi', 0.35, 'low'), ('AbuseIPDB', 75.0, 'high'), ('IPQS', None, None), ('DBIP', None, None)]
    assert got['risk'] == {'value': 75.0, 'level': 'high', 'sources': 4}
    # Factors: how many say yes of those that answered at all, and which.
    factors = {f['key']: (f['flagged'], f['total'], f['by']) for f in got['factors']}
    assert factors == {'Proxy': (1, 2, ['IPQS']), 'VPN': (2, 3, ['IPinfo', 'ipapi']), 'Tor': (0, 1, []), 'Server': (0, 0, []), 'Abuser': (1, 1, ['IPQS']), 'Robot': (0, 0, [])}
    assert [m['key'] for m in got['media']] == ['Netflix', 'ChatGPT']
    assert got['mail'] == {'port25': False, 'listed': 1, 'marked': 24, 'checked': 423}


def test_what_is_not_a_report():
    assert ip_quality.summarize(None) is None and ip_quality.summarize({'Head': {}}) is None and ip_quality.summarize([]) is None
    odd = copy.deepcopy(REPORT)
    odd['Info']['Latitude'] = '123'
    odd['Info']['Longitude'] = 'east'
    odd['Score'] = {'A': True, 'B': '12abc', 'C': '-3'}
    got = ip_quality.summarize(odd)
    assert (got['location']['lat'], got['location']['lon']) == (None, None)
    assert [s['value'] for s in got['scores']] == [None, None, None] and got['risk'] == {'value': None, 'level': None, 'sources': 0}


def test_purity_and_scenes():
    # Nothing against it, residential: as clean as it gets, every scene at its best.
    clean = copy.deepcopy(REPORT)
    clean['Score'] = {'IP2LOCATION': '0', 'SCAMALYTICS': '0', 'AbuseIPDB': 0}
    clean['Factor'] = {'Proxy': {'IPQS': False}, 'VPN': {'ipapi': False}, 'Abuser': {'IPQS': False}}
    clean['Mail']['DNSBlacklist'] = {'Total': 423, 'Clean': 423, 'Marked': 0, 'Blacklisted': 0}
    clean['Media'] = {'TikTok': {'Status': '解锁', 'Region': 'ES'}, 'ChatGPT': {'Status': '解锁', 'Region': 'ES'}}
    got = ip_quality.summarize(clean)
    assert got['purity'] == {'value': 100, 'label': '极度纯净'}
    assert [(x['key'], x['value'], x['label'], x['status']) for x in got['scenes']] == [
        ('tiktok', 10, '极佳', '解锁'), ('social', 10, '极佳', ''), ('ai', 10, '极佳', '解锁')]
    # A data centre's: a little risk, five of six calling it one, two blacklists listing it, 24 marking it;
    # Reddit blocked holds 社媒运营 down, not broadcast so nothing for that.
    dc = copy.deepcopy(clean)
    dc['Type'] = {'Usage': {'IPinfo': '机房', 'ipapi': '机房', 'IP2LOCATION': '机房'}, 'Company': {}}
    dc['Score'] = {'IP2LOCATION': '3', 'SCAMALYTICS': '0', 'ipapi': '0.00%', 'AbuseIPDB': 0, 'DBIP': 0}
    dc['Factor'] = {'Server': {'IP2LOCATION': True, 'ipapi': True, 'ipregistry': True, 'SCAMALYTICS': False, 'ipdata': True, 'IPinfo': True}}
    dc['Mail']['DNSBlacklist'] = {'Total': 423, 'Clean': 397, 'Marked': 24, 'Blacklisted': 2}
    dc['Media']['Reddit'] = {'Status': '屏蔽'}
    got = ip_quality.summarize(dc)
    assert got['purity'] == {'value': 83, 'label': '纯净'}
    assert [(x['key'], x['value'], x['label']) for x in got['scenes']] == [('tiktok', 7, '良好'), ('social', 3, '较差'), ('ai', 7, '良好')]
    # Flagged as much as the sample: nothing left; broadcast takes 3 more where there is anything to take.
    assert ip_quality.summarize(REPORT)['purity'] == {'value': 0, 'label': '高危'}
    broadcast = copy.deepcopy(dc)
    broadcast['Info']['Type'] = '广播IP'
    assert ip_quality.summarize(broadcast)['purity']['value'] == 80


def test_what_changed_since_the_run_before():
    before = ip_quality.summarize(REPORT)
    later = copy.deepcopy(REPORT)
    later['Score']['AbuseIPDB'] = 10
    later['Factor']['Proxy']['IP2LOCATION'] = True
    later['Media']['ChatGPT'] = {'Status': '解锁', 'Region': 'ES'}
    now = ip_quality.summarize(later)
    assert ip_quality.changes(now, before) == [
        {'kind': 'purity', 'name': '纯净分', 'from': 0, 'to': 11},
        {'kind': 'risk', 'name': '综合风险', 'from': 75.0, 'to': 34.0},
        {'kind': 'factor', 'name': '代理', 'from': 1, 'to': 2},
        {'kind': 'media', 'name': 'ChatGPT', 'from': '屏蔽', 'to': '解锁 ES'}]
    assert ip_quality.changes(now, None) == [] and ip_quality.changes(now, now) == []



def test_no_risk_data_is_not_clean():
    """The risk databases' lookups all failed or were refused: the script still writes a report, every score
    'null', no factor voted on. Nothing to rate rather than 100 (极度纯净), and no scenes from it; a report whose
    databases answered all zero is still 100."""
    blind = copy.deepcopy(REPORT)
    blind['Score'] = {name: 'null' for name in ('IP2LOCATION', 'SCAMALYTICS', 'ipapi', 'AbuseIPDB', 'IPQS', 'DBIP')}
    blind['Factor'] = {key: {'IPQS': None, 'ipapi': 'null'} for key, _ in ip_quality.FACTORS}
    blind['Media'] = {}
    got = ip_quality.summarize(blind)
    assert got['risk'] == {'value': None, 'level': None, 'sources': 0}
    assert got['purity'] == {'value': None, 'label': '无法评估'} and got['scenes'] == []
    clean = copy.deepcopy(blind)
    clean['Score'] = {'IP2LOCATION': '0', 'AbuseIPDB': 0}
    clean['Mail']['DNSBlacklist'] = {'Total': 423, 'Clean': 423, 'Marked': 0, 'Blacklisted': 0}
    assert ip_quality.summarize(clean)['purity'] == {'value': 100, 'label': '极度纯净'}
    # Only factor votes: rated on them.
    voted = copy.deepcopy(blind)
    voted['Factor'] = {'Proxy': {'IPQS': True, 'ipapi': False}}
    assert ip_quality.summarize(voted)['purity']['value'] == round(100 - 25 / 2 - min(1 * 2, 10) - min(24 * 0.2, 5))
    # Nothing rated is no change of the score either way.
    assert [c['kind'] for c in ip_quality.changes(got, ip_quality.summarize(clean))] == []


def test_what_the_script_could_not_find_and_its_colour_codes():
    """IP.Check.Place writes 'null' for what it could not find (city, time zone, coordinates), and its YouTube
    region came with colour codes missing their bracket: neither reaches the card."""
    bare = copy.deepcopy(REPORT)
    bare['Info'].update({'Latitude': 'null', 'Longitude': 'null', 'TimeZone': 'null', 'Map': 'null', 'Region': {'Code': 'SG', 'Name': '新加坡'},
                         'City': {'Name': 'null', 'PostalCode': 'null', 'SubCode': 'null', 'Subdivisions': 'null'}, 'Continent': {'Code': 'null', 'Name': 'null'}})
    bare['Media'] = {'Youtube': {'Status': '中国', 'Region': '\x1b31mCN\x1b32m', 'Type': ''}, 'Netflix': {'Status': '解锁', 'Region': '\x1b[32mSG\x1b[0m', 'Type': '原生'}}
    summary = ip_quality.summarize(bare)
    assert summary['location'] == {'lat': None, 'lon': None, 'city': '', 'subdivision': '', 'country': 'SG', 'countryName': '新加坡',
                                   'registered': 'ES', 'registeredName': '西班牙', 'timezone': ''}
    assert [(m['name'], m['status'], m['region']) for m in summary['media']] == [('Netflix', '解锁', 'SG'), ('YouTube', '中国', 'CN')]
    later = copy.deepcopy(bare)
    later['Media']['Youtube'] = {'Status': '解锁', 'Region': 'SG', 'Type': '原生'}
    assert ip_quality.changes(ip_quality.summarize(later), summary) == [{'kind': 'media', 'name': 'YouTube', 'from': '中国 CN', 'to': '解锁 SG'}]

async def run(e, sid, run_id, *, state='done', ended=0, report=REPORT, created=0):
    sections = zlib.compress(json.dumps([{'label': 'IPv4', 'ansi': '', 'json': report}] if report else [], ensure_ascii=False).encode())
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('INSERT INTO server_bench_runs(id,owner,server_id,test,options_json,package,suite,state,created_ms,ended_ms,sections) '
                           "VALUES(?,?,?,'ipcheck','{}','ip.check.place 1',?,?,?,?,?)", (run_id, OWNER, sid, '', state, created or ended, ended, sections))


async def test_the_card_reads_the_latest_run_and_one_under_way(env):  # noqa: F811
    sid = await server(env, 'ip-card', online=False)
    empty = await api(env, 'GET', f'/api/servers/{sid}/ip-quality')
    assert empty == {'ok': True, 'report': None, 'changes': [], 'previousAtMs': 0, 'pending': None}
    older = copy.deepcopy(REPORT)
    older['Media']['ChatGPT']['Status'] = '解锁'
    await run(env, sid, 'r-old', ended=1000, report=older)
    await run(env, sid, 'r-new', ended=2000)
    # A finished run without a report is passed over.
    await run(env, sid, 'r-empty', ended=3000, report=None)
    got = await api(env, 'GET', f'/api/servers/{sid}/ip-quality')
    assert (got['report']['runId'], got['report']['atMs'], got['report']['family'], got['previousAtMs']) == ('r-new', 2000, 'IPv4', 1000)
    assert got['changes'] == [{'kind': 'media', 'name': 'ChatGPT', 'from': '解锁', 'to': '屏蔽'}]
    assert got['pending'] is None
    await run(env, sid, 'r-going', state='running', created=4000)
    going = await api(env, 'GET', f'/api/servers/{sid}/ip-quality')
    assert going['pending'] == {'runId': 'r-going', 'state': 'running', 'startedMs': 4000} and going['report']['runId'] == 'r-new'
    await api(env, 'GET', '/api/servers/nope/ip-quality', status=404)


async def test_checked_by_itself(env):  # noqa: F811
    """Never checked first, at most two under way; an address that moved (as recorded, or as a run's masked
    address says) and a run older than the setting's days again; a failed run of its own left a while; only
    online Linux nodes that run commands, not in mainland China; nothing while switched off."""
    e = env
    checks = e.servers.ipchecks
    a = await server(e, 'ip-a', ipv4='203.0.113.5')
    b = await server(e, 'ip-b', ipv4='198.51.100.7')
    c = await server(e, 'ip-c', ipv4='192.0.2.9')
    await server(e, 'ip-win', platform='windows', ipv4='192.0.2.10')
    await server(e, 'ip-noexec', ipv4='192.0.2.11', exec_=False)
    await server(e, 'ip-off', ipv4='192.0.2.12', online=False)
    cn = await server(e, 'ip-cn', ipv4='192.0.2.13')
    await api(e, 'PATCH', f'/api/servers/{cn}', {'profile': {'region': 'CN'}})
    for sid in (a, b, c):
        e.servers.hub.sessions[sid].info['arch'] = 'amd64'
    # A platform 评测 has no build for: tried, then left alone a while, the others not held up.
    odd = await server(e, 'ip-mips', ipv4='192.0.2.14')
    e.servers.hub.sessions[odd].info['arch'] = 'mips'
    settings = (await api(e, 'GET', '/api/servers/settings'))['settings']
    assert settings['checks']['ipcheck'] is True and settings['ipcheckDays'] == 7

    async def runs():
        rows = await many(e.db.conn, "SELECT id, server_id, state FROM server_bench_runs WHERE test='ipcheck' ORDER BY created_ms")
        return [(r['server_id'], r['state']) for r in rows]

    async def finish(sid, ended=None, report=REPORT):
        async with e.db.write_transaction(label='test') as conn:
            await conn.execute("UPDATE server_bench_runs SET state='done', ended_ms=?, sections=? WHERE server_id=? AND state='queued'",
                               (ended or int(time.time() * 1000), zlib.compress(json.dumps([{'label': 'IPv4', 'json': report}]).encode()), sid))
    with unittest.mock.patch.object(e.servers.bench, '_kick', lambda sid: None):
        await checks.tick()
        first = {sid for sid, _ in await runs()}
        assert len(first) == 2 and first <= {a, b, c}
        await checks.tick()
        assert len(await runs()) == 2, 'no room'
        for sid in first:
            await finish(sid)
        await checks.tick()
        assert {sid for sid, _ in await runs()} == {a, b, c}
        await finish(({a, b, c} - first).pop())
        await checks.tick()
        assert len(await runs()) == 3, 'all fresh'
        assert json.loads(await e.servers.metrics.meta(f'ipcheck:{odd}'))['error'] == 'bench_platform_unsupported'
        assert await checks.why(odd, '192.0.2.14', 7) == ''
        # Its address moved (a dynamic IP): checked again.
        e.servers.hub.sessions[a].info['ipv4'] = '203.0.113.99'
        assert await checks.why(a, '203.0.113.99', 7) == 'moved'
        await checks.tick()
        assert (await runs())[-1] == (a, 'queued')
        await finish(a)
        # A run it did not start: the masked address it was for (203.0.*.*) is all there is to go by.
        mine = copy.deepcopy(REPORT)
        mine['Head']['IP'] = '192.0.*.*'
        await run(e, c, 'manual', ended=int(time.time() * 1000), created=int(time.time() * 1000) + 1, report=mine)
        assert await checks.why(c, '192.0.2.9', 7) == '' and await checks.why(c, '192.0.77.1', 7) == '' and await checks.why(c, '198.51.100.1', 7) == 'moved'
        # Older than the setting's days: again, unless the days are more.
        week_ago = int(time.time() * 1000) - 8 * 86400000
        async with e.db.write_transaction(label='test') as conn:
            await conn.execute("UPDATE server_bench_runs SET ended_ms=? WHERE server_id=?", (week_ago, b))
        assert await checks.why(b, '198.51.100.7', 7) == 'old' and await checks.why(b, '198.51.100.7', 30) == ''
        await api(e, 'PUT', '/api/servers/settings', {'ipcheckDays': 30})
        await checks.tick()
        assert len(await runs()) == 5
        assert (await api(e, 'PUT', '/api/servers/settings', {'ipcheckDays': 3}, status=400))['error'] == 'invalid_ipcheck_days'
        await api(e, 'PUT', '/api/servers/settings', {'ipcheckDays': 7})
        await checks.tick()
        assert (await runs())[-1] == (b, 'queued')
        # Its own run failed: left alone a while, then tried again.
        async with e.db.write_transaction(label='test') as conn:
            await conn.execute("UPDATE server_bench_runs SET state='failed', ended_ms=? WHERE server_id=? AND state='queued'", (int(time.time() * 1000), b))
        await checks.tick()
        assert (await runs())[-1] == (b, 'failed')
        assert await checks.why(b, '198.51.100.7', 7, now=int(time.time() * 1000) + checks.FAILED_S * 1000 + 1) == 'old'
        # Switched off: nothing starts by itself.
        await api(e, 'PUT', '/api/servers/settings', {'checks': {'ipcheck': False}})
        e.servers.hub.sessions[c].info['ipv4'] = '198.51.100.200'
        await checks.tick()
        assert (await runs())[-1] == (b, 'failed')


async def test_servers_coming_online_together_start_no_more_than_the_room(env):  # noqa: F811
    """11-12 review: each hello counted the same room before any had taken it."""
    e = env
    checks = e.servers.ipchecks
    ids = [await server(e, f'ip-{i}', ipv4=f'203.0.113.{10 + i}') for i in range(5)]
    for sid in ids:
        e.servers.hub.sessions[sid].info['arch'] = 'amd64'
    with unittest.mock.patch.object(e.servers.bench, '_kick', lambda sid: None):
        await asyncio.gather(*(checks.check(sid) for sid in ids), checks.tick())
    rows = await many(e.db.conn, "SELECT COUNT(*) AS n FROM server_bench_runs WHERE test='ipcheck'")
    assert rows[0]['n'] == checks.AT_ONCE


async def sections_run(e, sid, run_id, sections, *, ended):
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('INSERT INTO server_bench_runs(id,owner,server_id,test,options_json,package,suite,state,created_ms,ended_ms,sections) '
                           "VALUES(?,?,?,'ipcheck','{}','ip.check.place 1','','done',?,?,?)",
                           (run_id, OWNER, sid, ended, ended, zlib.compress(json.dumps(sections, ensure_ascii=False).encode())))


def scored(risk, ip):
    report = copy.deepcopy(REPORT)
    report['Head']['IP'] = ip
    report['Score'] = {'IP2LOCATION': risk, 'AbuseIPDB': risk}
    return report


async def test_the_card_keeps_to_one_address(env):  # noqa: F811
    """A dual-stack server checked over IPv4, then over IPv6 alone; then its IPv4 changed. The card stays on the
    IPv4 reports, compares a report only with an earlier one of the same address, and says when the report is
    not for the address the server has now."""
    e = env
    sid = await server(e, 'dual', ipv4='203.0.113.5', online=False)
    await sections_run(e, sid, 'v4-old', [{'label': 'IPv4', 'json': scored('10', '203.0.*.*')}], ended=1000)
    await sections_run(e, sid, 'v6', [{'label': 'IPv6', 'json': scored('100', '2a0e:97c0:3e0:*:*:*:*:*')}], ended=2000)
    got = await api(e, 'GET', f'/api/servers/{sid}/ip-quality')
    assert (got['report']['runId'], got['report']['family'], got['report']['ip'], got['report']['moved']) == ('v4-old', 'IPv4', '203.0.*.*', False)
    assert got['changes'] == [] and got['previousAtMs'] == 0, 'the IPv6 report is no change of the IPv4 address'
    # A dual-stack run: its IPv4 section is the card's, compared with the IPv4 one before.
    await sections_run(e, sid, 'both', [{'label': 'IPv4', 'json': scored('30', '203.0.*.*')}, {'label': 'IPv6', 'json': scored('0', '2a0e:*')}], ended=3000)
    got = await api(e, 'GET', f'/api/servers/{sid}/ip-quality')
    assert (got['report']['runId'], got['report']['family'], got['previousAtMs']) == ('both', 'IPv4', 1000)
    assert {c['kind']: (c['from'], c['to']) for c in got['changes']}['risk'] == (10.0, 30.0)
    # Its address moved to another network; the report there is not compared with the old address's.
    await sections_run(e, sid, 'moved', [{'label': 'IPv4', 'json': scored('80', '198.51.*.*')}], ended=4000)
    got = await api(e, 'GET', f'/api/servers/{sid}/ip-quality')
    assert got['report']['runId'] == 'moved' and got['changes'] == [] and got['previousAtMs'] == 0
    assert got['report']['moved'] is True, 'the server is still on 203.0.113.5'
    e.servers.hub.sessions.pop(sid, None)
    await e.servers._merge_node_info(sid, {'info': {'platform': 'linux', 'ipv4': '198.51.100.7'}})
    assert (await api(e, 'GET', f'/api/servers/{sid}/ip-quality'))['report']['moved'] is False
    # The full address of each run IpChecks started tells apart two of one network.
    direct = await ip_quality.latest(e.db.conn, OWNER, sid, ips={'moved': '198.51.100.7', 'both': '198.51.100.99'})
    assert direct['report']['ip'] == '198.51.100.7' and direct['changes'] == []
    # A server with only IPv6 reports shows those.
    v6 = await server(e, 'v6only', online=False)
    await sections_run(e, v6, 'a', [{'label': 'IPv6', 'json': scored('10', '2a0e:97c0:3e0:*')}], ended=1000)
    await sections_run(e, v6, 'b', [{'label': 'IPv6', 'json': scored('20', '2a0e:97c0:3e0:*')}], ended=2000)
    got = await api(e, 'GET', f'/api/servers/{v6}/ip-quality')
    assert (got['report']['runId'], got['report']['family'], got['previousAtMs']) == ('b', 'IPv6', 1000)


async def test_an_ipv6_run_does_not_make_ipv4_moved(env):  # noqa: F811
    e = env
    sid = await server(e, 'v6-run', ipv4='203.0.113.5', online=False)
    now = int(time.time() * 1000)
    await sections_run(e, sid, 'v4', [{'label': 'IPv4', 'json': scored('0', '203.0.*.*')}], ended=now - 1000)
    await sections_run(e, sid, 'v6', [{'label': 'IPv6', 'json': scored('0', '2a0e:97c0:3e0:*')}], ended=now)
    assert await e.servers.ipchecks.why(sid, '203.0.113.5', 7) == ''
