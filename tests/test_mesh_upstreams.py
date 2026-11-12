"""Members' upstream relays and direct switch, and where a link came from: the document fields, the
migration of fixed and failover entries in the draft, and what nodes are sent until data planes
route over several upstreams."""
from __future__ import annotations

import pytest

from app.servers import mesh_auto
from app.servers import mesh_model as model
from app.servers.mesh_config import build, node_entry_mode
from tests.test_mesh_config import doc, errors, facts, member, pair

ALL = {'udp': 51999, 'tcp': 52000, 'tls': 52001}


def entry(mode, *relays):
    return {'mode': mode, 'candidates': [{'id': f'c{i + 1}', 'relay': r, 'transport': 'udp'} for i, r in enumerate(relays)], 'autoReturn': False}


def test_a_new_member_has_upstreams_and_may_be_linked_directly():
    m = model.new_member('m1', model.default_settings())
    assert m['entry']['mode'] == 'upstreams' and m['direct'] is True


def test_an_old_member_may_be_linked_directly_once_loaded():
    old = model.empty_doc()
    old['members'] = [{k: v for k, v in member('m1').items() if k != 'direct'}]
    assert model.upgrade(old)['members'][0]['direct'] is True


def test_nodes_run_upstreams_as_a_failover_that_returns_to_the_first():
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('m1', entry=entry('upstreams', 'r1', 'r2')))
    sent = build(d, facts(), 'net', 1)['m1']['entry']
    assert sent['mode'] == 'failover' and sent['autoReturn'] is True and [c['relay'] for c in sent['candidates']] == ['r1', 'r2']
    # Fixed and failover entries still run as they were set.
    for mode in ('fixed', 'failover'):
        d['members'][2]['entry'] = entry(mode, 'r1', 'r2')
        assert build(d, facts(), 'net', 1)['m1']['entry']['mode'] == mode
        assert node_entry_mode(d['members'][2]) == (mode, False)


def test_the_draft_keeps_fixed_and_failover_entries_until_the_user_changes_them():
    applied = doc(member('r1', 'relay'), member('m1', entry=entry('fixed', 'r1')), member('m2', entry=entry('failover', 'r1')))
    draft = mesh_auto.for_draft(applied)
    assert [m['entry'] for m in draft['members']] == [m['entry'] for m in applied['members']]
    assert [m['entry']['mode'] for m in draft['members']] == ['upstreams', 'fixed', 'failover']


def test_the_draft_takes_direct_and_a_mode_of_upstreams():
    d = doc(member('r1', 'relay'), member('m1', entry=entry('fixed', 'r1')))
    clean = model.clean_doc({'members': [{'serverId': 'r1'}, {'serverId': 'm1', 'direct': False, 'entry': {'mode': 'upstreams'}}]}, d)
    assert clean['members'][1]['direct'] is False and clean['members'][1]['entry']['mode'] == 'upstreams'
    for raw, path in (({'direct': 'no'}, 'members[1].direct'), ({'entry': {'mode': 'all'}}, 'members[1].entry.mode')):
        with pytest.raises(model.MeshInvalid) as bad:
            model.clean_doc({'members': [{'serverId': 'r1'}, {'serverId': 'm1', **raw}]}, d)
        assert path in {e['path'] for e in bad.value.errors}


def test_a_link_the_mesh_made_says_why_and_since_when():
    d = doc(member('r1', 'relay'), member('r2', 'relay'), member('m1'), member('m2'))
    made = {**pair('r1', 'r2'), 'bias': 0, 'origin': 'auto', 'reason': 'M1 → M2 271 → 172ms', 'since': 1794333000000}
    clean = model.clean_doc({'relayLinks': {'mode': 'custom', 'pairs': [made, {**pair('r2', 'r1'), 'origin': 'manual'}]},
                             'directLinks': [{'a': 'm1', 'b': 'm2', 'transport': 'udp', 'origin': 'auto', 'reason': '同一机房', 'since': 1}]}, d)
    assert clean['relayLinks']['pairs'][0] == made
    # By hand is the default and is not stored.
    assert 'origin' not in clean['relayLinks']['pairs'][1] and clean['directLinks'][0]['origin'] == 'auto'
    for raw, path in (({'origin': 'robot'}, 'relayLinks.pairs[0].origin'), ({'origin': 'auto', 'since': -1}, 'relayLinks.pairs[0].reason')):
        with pytest.raises(model.MeshInvalid) as bad:
            model.clean_doc({'relayLinks': {'mode': 'custom', 'pairs': [{**pair('r1', 'r2'), **raw}]}}, d)
        assert path in {e['path'] for e in bad.value.errors}


def test_a_limit_to_links_set_by_hand_names_a_link_the_mesh_made_across():
    f = facts()
    f['r1']['region'], f['r2']['region'] = 'CN', 'US'
    d = doc(member('r1', 'relay', listen=ALL), member('r2', 'relay', listen=ALL),
            relay_links={'mode': 'custom', 'pairs': [{**pair('r1', 'r2', transport='tcp'), 'bias': 0, 'origin': 'auto', 'reason': '', 'since': 0}]})
    d['limits'] = [{'id': 'k1', 'name': '跨境', 'on': True, 'a': {'kind': 'region', 'regions': ['CN']}, 'b': {'kind': 'region', 'regions': ['CN'], 'not': True},
                    'relayEnd': False, 'transports': None, 'via': None, 'manualOnly': True}]
    assert ('relayLinks.pairs[0]', 'limit_violation') in errors(d, f)
    del d['relayLinks']['pairs'][0]['origin']
    assert ('relayLinks.pairs[0]', 'limit_violation') not in errors(d, f)
