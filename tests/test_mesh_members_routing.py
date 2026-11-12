"""Tests for member routing format and mixed-version compatibility in mesh_config.build."""
from __future__ import annotations

import copy

from app.servers import compat
from app.servers import mesh_model as model
from app.servers.mesh_config import MEMBERS_ROUTING_FEATURE, build


KEYS = {sid: (f'{sid}wg'.ljust(43, 'A') + '=', f'{sid}ctl'.ljust(43, 'B') + '=', f'{sid}sig'.ljust(43, 'C') + '=', f'{sid}e2e'.ljust(43, 'D') + '=')
        for sid in ('r1', 'r2', 'm1', 'm2')}


def _member(sid, role='member', **changes):
    m = model.new_member(sid, model.default_settings())
    m['role'] = role
    for key, value in changes.items():
        m[key] = {**m[key], **value} if isinstance(m[key], dict) and isinstance(value, dict) else value
    return m


def _doc(*members, direct=()):
    d = model.empty_doc()
    d['members'] = [copy.deepcopy(m) for m in members]
    d['directLinks'] = list(direct)
    return model.allocate(d)


def _facts(featured=()):
    return {sid: {'name': sid.upper(), 'platform': 'linux', 'ipv4': f'203.0.113.{i + 1}',
                  'publicKey': KEYS[sid][0], 'controlKey': KEYS[sid][1], 'signKey': KEYS[sid][2], 'e2eKey': KEYS[sid][3],
                  'localPrefixes': [], 'configSchemas': compat.CONFIG_SCHEMA,
                  'features': [MEMBERS_ROUTING_FEATURE] if sid in featured else []}
            for i, sid in enumerate(KEYS)}


def test_member_routing_enabled_when_member_and_all_upstreams_report_feature():
    d = _doc(
        _member('r1', 'relay', listen={'tcp': 52000}),
        _member('r2', 'relay', listen={'udp': 51999}),
        _member('m1', entry={'mode': 'upstreams', 'candidates': [
            {'id': 'c1', 'relay': 'r1', 'transport': 'tcp'},
            {'id': 'c2', 'relay': 'r2', 'transport': 'udp'},
        ], 'autoReturn': False}),
        _member('m2', listen={'tcp': 52000}, entry={'mode': 'fixed', 'candidates': [
            {'id': 'c1', 'relay': 'r1', 'transport': 'tcp'},
        ], 'autoReturn': False}),
        direct=[{'a': 'm1', 'b': 'm2', 'transport': 'tcp', 'dialer': 'a', 'bias': 3}],
    )
    configs = build(d, _facts(featured=('r1', 'r2', 'm1')), 'net-1', 1)

    m1 = configs['m1']
    assert m1['self']['leaf'] is True
    assert m1['entry'] is None
    assert m1['links'] == [
        {'peer': 'm2', 'kind': 'direct', 'dial': True, 'endpoint': 'tcp://203.0.113.4:52000'},
        {'peer': 'r1', 'kind': 'relay', 'dial': True, 'endpoint': 'tcp://203.0.113.1:52000'},
        {'peer': 'r2', 'kind': 'relay', 'dial': True, 'endpoint': 'udp://203.0.113.2:51999'},
    ]
    assert m1['routing'] == {'mode': 'dynamic', 'bias': {'m2': 3, 'r1': 0, 'r2': 0}}
    assert m1['relayLinks'] == [['r1', 'r2']]
    assert m1['memberLinks'] == [['m1', 'r1'], ['m1', 'r2']]

    r1 = configs['r1']
    assert {'peer': 'm1', 'kind': 'member', 'dial': False} in r1['links']
    assert r1['memberLinks'] == [['m1', 'r1'], ['m1', 'r2']]
    assert next(n for n in r1['nodes'] if n['id'] == 'm1').get('leaf') is True

    # m2 is in fixed mode (and not featured): keeps legacy entry format.
    m2 = configs['m2']
    assert 'leaf' not in m2['self']
    assert m2['entry'] is not None and m2['entry']['mode'] == 'fixed'
    assert 'routing' not in m2


def test_mixed_versions_fallback_to_legacy_entry_until_member_and_all_upstreams_support_feature():
    d = _doc(
        _member('r1', 'relay', listen={'tcp': 52000}),
        _member('r2', 'relay', listen={'udp': 51999}),
        _member('m1', entry={'mode': 'upstreams', 'candidates': [
            {'id': 'c1', 'relay': 'r1', 'transport': 'tcp'},
            {'id': 'c2', 'relay': 'r2', 'transport': 'udp'},
        ], 'autoReturn': False}),
    )
    # 1. Member is new, r1 is new, but r2 is old -> m1 gets legacy failover+autoReturn format.
    configs = build(d, _facts(featured=('r1', 'm1')), 'net-1', 1)
    assert 'leaf' not in configs['m1']['self']
    assert configs['m1']['entry']['mode'] == 'failover'
    assert configs['m1']['entry']['autoReturn'] is True
    assert len(configs['m1']['entry']['candidates']) == 2
    assert configs['m1']['links'] == []
    assert all(l['peer'] != 'm1' for l in configs['r1']['links'])

    # 2. Both relays are new, but m1 is old -> m1 gets legacy failover+autoReturn format, relays use owner protocol for m1.
    configs = build(d, _facts(featured=('r1', 'r2')), 'net-1', 1)
    assert 'leaf' not in configs['m1']['self']
    assert configs['m1']['entry'] is not None
    assert all(l['peer'] != 'm1' for l in configs['r1']['links'])
    assert all(l['peer'] != 'm1' for l in configs['r2']['links'])

    # 3. m1 only lists r1 as upstream and both m1 & r1 are new, while sibling relay r2 is still on 1.2.0:
    #    m1 stays on legacy entry until r2 upgrades too, so r1 never emits member compact adverts that r2 rejects.
    d_single = _doc(
        _member('r1', 'relay', listen={'tcp': 52000}),
        _member('r2', 'relay', listen={'udp': 51999}),
        _member('m1', entry={'mode': 'upstreams', 'candidates': [
            {'id': 'c1', 'relay': 'r1', 'transport': 'tcp'},
        ], 'autoReturn': False}),
    )
    mixed = build(d_single, _facts(featured=('r1', 'm1')), 'net-1', 1)
    assert 'leaf' not in mixed['m1']['self']
    assert mixed['m1']['entry'] is not None
    assert 'memberLinks' not in mixed['r1'] and 'memberLinks' not in mixed['r2']
    assert all(l['peer'] != 'm1' for l in mixed['r1']['links'])

    upgraded = build(d_single, _facts(featured=('r1', 'r2', 'm1')), 'net-1', 2)
    assert upgraded['m1']['self']['leaf'] is True
    assert upgraded['r1']['memberLinks'] == [['m1', 'r1']]
