"""Builds config.json for every node of the contract test with the controller's own code.

Run from the repository root: .venv/bin/python node/mesh/testdata/contract_configs.py IN OUT
IN holds the keys, the config schemas the data plane reads, underlay addresses, relay TLS fingerprint, relay link transport and
revision, and optionally keyless: members whose signing and end-to-end keys the controller does not know; OUT gets {"configs": {serverId: config}, "errors": [...]} where errors are what
the controller's own draft check reports.
"""
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from app.servers import mesh_model as model  # noqa: E402
from app.servers.mesh_config import build  # noqa: E402


def member(sid, role='member', **changes):
    m = model.new_member(sid, model.default_settings())
    m['role'] = role
    m.update(changes)
    return m


def rule(rid, src, dst, proto, ports=()):
    return {'id': rid, 'src': {'members': [], 'tags': [], 'prefixes': [], **src},
            'dst': {'members': [], 'tags': [], 'subnets': [], **dst}, 'proto': proto, 'ports': [list(p) for p in ports], 'note': ''}


def main(src, dst):
    with open(src) as f:
        given = json.load(f)
    doc = model.empty_doc()
    doc['members'] = [
        member('r1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0}),
        member('r2', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 52001}),
        member('m1', tags=['office'], entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}], 'autoReturn': True}),
        member('m2', tags=['office'], entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'tcp'}], 'autoReturn': True}),
        member('m3', gateway={'subnets': ['10.98.0.0/24'], 'masquerade': True},
               entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r2', 'transport': 'tls'}], 'autoReturn': True}),
    ]
    doc['relayLinks'] = {'mode': 'custom', 'pairs': [{'a': 'r1', 'b': 'r2', 'transport': given['relayTransport'], 'cost': 1, 'dialer': 'a'}]}
    doc['directLinks'] = [{'a': 'm1', 'b': 'm2', 'transport': 'udp', 'dialer': 'a'}]
    doc['rules'] = [
        rule('ping', {'tags': ['office'], 'members': ['m3']}, {'tags': ['office'], 'members': ['m3']}, 'icmp'),
        rule('web', {'members': ['m1']}, {'members': ['m3']}, 'tcp', [(7000, 7000)]),
        rule('lan', {'members': ['m1']}, {'subnets': ['10.98.0.0/24']}, 'any'),
    ]
    doc = model.allocate(doc)
    facts = {sid: {'name': sid.upper(), 'platform': 'linux', 'ipv4': given['ipv4'][sid], 'publicKey': keys[0], 'controlKey': keys[1],
                   'signKey': keys[2], 'e2eKey': keys[3], 'localPrefixes': [], 'tls': None,
                   'configSchemas': given['configSchemas']}
             for sid, keys in given['keys'].items()}
    for sid in given.get('keyless') or []:
        facts[sid]['signKey'] = facts[sid]['e2eKey'] = ''
    for sid, fingerprint in given['tlsFingerprint'].items():
        facts[sid]['tls'] = {'active': {'id': 'active', 'fingerprint': fingerprint}}
    errors = model.validate(doc, facts, applying=True)
    configs = build(doc, facts, 'oblab-contract', given['revision'])
    with open(dst, 'w') as f:
        json.dump({'configs': configs, 'errors': errors}, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
