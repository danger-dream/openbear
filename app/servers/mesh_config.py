"""Routes and per-node configs (config.json, schema 1). Pure functions of the mesh
document and the facts nodes reported, so the same input always gives the same configs."""
from __future__ import annotations

import base64
import binascii
import contextlib
import copy
import hashlib
import heapq
import ipaddress
import itertools
import json
import re

from app.servers import compat

# The config.json schemas this controller writes, (min, max).
SCHEMA = compat.CONFIG_SCHEMA
# What a data plane from before configSchemas reads.
LEGACY_SCHEMAS = (1, 1)
TRANSPORTS = ('udp', 'tcp', 'tls')
HEALTH = {'activeIntervalMs': 1000, 'standbyIntervalMs': 2000, 'coldRetryMs': 30000, 'initialTimeoutMs': 3000, 'minTimeoutMs': 2000,
          'maxTimeoutMs': 10000, 'consecutiveFailures': 3, 'readySuccesses': 3, 'handshakeGraceMs': 6000, 'returnHealthyMs': 60000,
          'minResidenceMs': 60000, 'leaseMs': 30000, 'leaseRefreshMs': 5000}


# Reflectors the data plane accepts.
REFLECTORS_KEPT = 16
# Container bridges every docker / podman host has by default: an address there says
# nothing about which machines share a network.
BRIDGE_NETS = tuple(ipaddress.ip_network(n) for n in ('172.17.0.0/16', '10.88.0.0/16'))
# Private (RFC 1918) networks: where machines on one LAN sit.
LAN_NETS = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


_HOSTNAME = re.compile(r'(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?')


def valid_host(host):
    """An IP literal or a DNS name: what an endpoint or a reflector may aim at. Anything else
    (a node's report gone wrong) would have the node refuse its whole config."""
    if not isinstance(host, str) or not host:
        return False
    try:
        return not ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return bool(_HOSTNAME.fullmatch(host))


def is_lan(ip):
    return ip.version == 4 and any(ip in n for n in LAN_NETS)


def is_site(member):
    """A site relay sits behind NAT: it is reached by punching only."""
    return member['role'] == 'relay' and member.get('relayMode') == 'site'


def endpoint_host(member, facts):
    """Where others reach the member: what the user entered, else the IPv4 its node reports.
    A site relay has none: the IPv4 the panel has for a machine behind a proxy is the proxy's."""
    if is_site(member):
        return ''
    host = member['publicEndpoint'] or (facts.get(member['serverId']) or {}).get('ipv4', '')
    return host if valid_host(host) else ''


def punch_host(member, facts):
    """Where a punched link or candidate may aim at the member before punching: what the user
    entered, else the public IP its own reachability check found (verdict public, seen the
    same by every observer). Never the panel's IPv4: behind a proxy that is the proxy's, and
    WireGuard handshakes would go to a third party. Without either, reflectors find the way."""
    host = member['publicEndpoint'] or (facts.get(member['serverId']) or {}).get('publicIp', '')
    return host if valid_host(host) else ''


def punch_endpoint(member, facts):
    host = punch_host(member, facts)
    return endpoint('udp', host, member['listen']['udp']) if host and member['listen']['udp'] else ''


def can_dial(target, transport, facts):
    return bool(target['listen'][transport] and endpoint_host(target, facts))


def address(host, port):
    return f"{f'[{host}]' if ':' in host else host}:{port}"


def endpoint(transport, host, port):
    return f'{transport}://{address(host, port)}'


def dial_target(target, transport, facts):
    """(endpoint, tlsPins) to dial the member over transport, or None while that cannot be
    written: no address known (yet), or TLS without a pinned certificate. A config never
    carries tcp://:port, which the node refuses whole; the draft check reports the cause."""
    host = endpoint_host(target, facts)
    pins = tls_pins(facts, target['serverId']) if transport == 'tls' else []
    if not host or not target['listen'][transport] or transport == 'tls' and not pins:
        return None
    return endpoint(transport, host, target['listen'][transport]), pins


def reflectors(members, facts, first=(), exclude=None):
    """UDP addresses of the relays that can show a node its public address: public relays
    listening on UDP, except the node itself, those in first in its order, then the others
    by id."""
    usable = {m['serverId']: m for m in members if m['role'] == 'relay' and m['serverId'] != exclude and can_dial(m, 'udp', facts)}
    order = [sid for sid in dict.fromkeys(first) if sid in usable] + sorted(sid for sid in usable if sid not in first)
    return list(dict.fromkeys(address(endpoint_host(usable[sid], facts), usable[sid]['listen']['udp']) for sid in order))[:REFLECTORS_KEPT]


def tls_pins(facts, server_id):
    """The active certificate's fingerprint and, during a rotation, the pending one's."""
    tls = (facts.get(server_id) or {}).get('tls') or {}
    return [c['fingerprint'] for c in (tls.get('active'), tls.get('pending')) if c and c.get('fingerprint')][:2]


def clean_schemas(raw):
    """configSchemas a data plane reports, [min, max], as a tuple; None when absent or malformed."""
    if (isinstance(raw, (list, tuple)) and len(raw) == 2 and all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10000 for v in raw)
            and raw[0] <= raw[1]):
        return raw[0], raw[1]
    return None


def schemas_of(facts, sid):
    """The config schemas a member's data plane reads; unknown counts as the first one."""
    return tuple((facts.get(sid) or {}).get('configSchemas') or LEGACY_SCHEMAS)


def config_schema(doc, facts):
    """The highest schema this controller and every member read, or None when there is none."""
    ranges = [schemas_of(facts, m['serverId']) for m in _live(doc)]
    return compat.common(SCHEMA, (max((r[0] for r in ranges), default=SCHEMA[0]), min((r[1] for r in ranges), default=SCHEMA[1])))


def config_hash(config):
    body = {k: v for k, v in config.items() if k != 'revision'}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _live(doc):
    return [m for m in doc['members'] if not m['removing']]


def relay_pairs(doc, facts):
    """Relay-to-relay links in effect. Custom: the listed pairs. Full: every pair of relays,
    a listed pair overriding the default for its two relays."""
    live = {m['serverId']: m for m in _live(doc)}
    relays = sorted(sid for sid, m in live.items() if m['role'] == 'relay')
    listed = {}
    for pair in doc['relayLinks']['pairs']:
        if pair['a'] != pair['b'] and pair['a'] in relays and pair['b'] in relays:
            listed.setdefault(frozenset((pair['a'], pair['b'])), pair)
    if doc['relayLinks']['mode'] == 'custom':
        return [effective(p, live) for p in listed.values()]
    out = []
    for a, b in itertools.combinations(relays, 2):
        out.append(effective(listed.get(frozenset((a, b))) or _default_pair(live[a], live[b], facts), live))
    return out


def effective(pair, live):
    """A relay link as it runs: two site relays punch it; between a site relay and a public
    one the site relay dials, whatever the pair says."""
    out = {**pair, 'bias': pair.get('bias', 0)}
    a, b = live.get(pair['a']), live.get(pair['b'])
    if a and b and is_site(a) and is_site(b):
        out['punch'] = True
    elif a and b and is_site(a) != is_site(b):
        out['dialer'] = 'a' if is_site(a) else 'b'
    return out


def _default_pair(a, b, facts):
    for transport in TRANSPORTS:
        for dialer, target in (('a', b), ('b', a)):
            if can_dial(target, transport, facts):
                return {'a': a['serverId'], 'b': b['serverId'], 'transport': transport, 'cost': 1, 'dialer': dialer}
    return {'a': a['serverId'], 'b': b['serverId'], 'transport': 'udp', 'cost': 1, 'dialer': 'a'}


def next_hops(relays, pairs):
    """For each relay, the neighbour relay to send to for every other relay it can reach.
    Every hop picks a neighbour on a shortest path, so the remaining distance shrinks at
    each hop and forwarding never loops; ties go to the smaller id."""
    adjacent = {r: {} for r in relays}
    for pair in pairs:
        a, b = pair['a'], pair['b']
        if a in adjacent and b in adjacent and a != b:
            cost = min(pair.get('cost', 1), adjacent[a].get(b, pair.get('cost', 1)))
            adjacent[a][b] = adjacent[b][a] = cost
    hops = {r: {} for r in relays}
    for target in sorted(relays):
        distance = {target: 0}
        queue = [(0, target)]
        while queue:
            d, node = heapq.heappop(queue)
            if d > distance[node]:
                continue
            for neighbour, cost in adjacent[node].items():
                if d + cost < distance.get(neighbour, float('inf')):
                    distance[neighbour] = d + cost
                    heapq.heappush(queue, (d + cost, neighbour))
        for source in relays:
            if source == target or source not in distance:
                continue
            hops[source][target] = min((cost + distance[n], n) for n, cost in adjacent[source].items() if n in distance)[1]
    return hops


def named(side, m):
    """Whether a rule side names a member itself: all (every member not being removed), its id or a tag of its."""
    return bool(side.get('all')) or m['serverId'] in side['members'] or bool(set(m['tags']) & set(side['tags']))


def side_nets(side, key, live):
    """A rule side's networks (src.prefixes or dst.subnets) with allSubnets turned into every
    gateway subnet of the live members ({serverId: member} or a list of them)."""
    out = list(side[key])
    if side.get('allSubnets'):
        out += [s for m in (live.values() if isinstance(live, dict) else live) for s in (m['gateway'] or {}).get('subnets', [])]
    return list(dict.fromkeys(out))


def _sources(side, live):
    """A member or tag stands for the machines themselves, a gateway's subnets included only
    when src.prefixes (or allSubnets) names them, as on the dst side: choosing a gateway machine
    for one rule must not open the network behind it to that rule's destinations. all names
    every member."""
    out = []
    for m in live.values():
        if named(side, m):
            out.append(f"{m['address']}/32")
    out += side_nets(side, 'prefixes', live)
    return list(dict.fromkeys(out))


def instance_access(doc, facts=None):
    """The automatic rules for instances whose port listens on their server's mesh address (facts:
    instancePorts, by server, bind the address it listens on): the gateway's server alone reaches each port, tcp, from its mesh
    address to the instance server's. [{serverId, gateway, instanceId, name, port, src, dst}] in
    server and port order; none while either end is not a live member with an address."""
    live = {m['serverId']: m for m in _live(doc)}
    out = []
    for sid, f in (facts or {}).items():
        me = live.get(sid)
        for entry in (f or {}).get('instancePorts') or []:
            gateway = live.get(entry['gateway'])
            # Only on the address it listens on: one the server no longer has gets nothing.
            if me and gateway and me['address'] and gateway['address'] and entry['gateway'] != sid and entry.get('bind') == me['address']:
                out.append({'serverId': sid, 'gateway': entry['gateway'], 'instanceId': entry['id'], 'name': entry['name'],
                            'port': entry['port'], 'src': f"{gateway['address']}/32", 'dst': f"{me['address']}/32"})
    return sorted(out, key=lambda a: (a['serverId'], a['port'], a['instanceId']))


def node_rules(doc, server_id, facts=None):
    """Rules this node enforces as a destination, with members and tags turned into networks.
    dst is never empty: the data plane reads an empty dst as every local address, gateway
    subnets included, so choosing a gateway machine would open the network behind it.
    The controller's machine (facts give it a controllerPort) first lets every member reach its
    port on its mesh address, whatever the user's rules say: their control connections may
    come that way. Instances on this machine listening on its mesh address are reached by the
    gateway's server alone, at their ports (instance_access), also whatever the user's rules say."""
    live = {m['serverId']: m for m in _live(doc)}
    me = live[server_id]
    mine = [ipaddress.ip_network(s) for s in (me['gateway'] or {}).get('subnets', [])]
    out = []
    port = ((facts or {}).get(server_id) or {}).get('controllerPort')
    members = [f"{m['address']}/32" for sid, m in live.items() if sid != server_id and m['address']]
    if port and me['address'] and members:
        out.append({'src': members, 'dst': [f"{me['address']}/32"], 'proto': 'tcp', 'ports': [[port, port]]})
    instances = {}
    for a in instance_access(doc, {server_id: (facts or {}).get(server_id)}):
        instances.setdefault(a['src'], set()).add(a['port'])
    for src in sorted(instances):
        out.append({'src': [src], 'dst': [f"{me['address']}/32"], 'proto': 'tcp', 'ports': [[p, p] for p in sorted(instances[src])]})
    for rule in doc['rules']:
        dst = rule['dst']
        targets = [f"{me['address']}/32"] if named(dst, me) else []
        targets += [s for s in side_nets(dst, 'subnets', live) if any(ipaddress.ip_network(s).subnet_of(g) for g in mine)]
        sources = _sources(rule['src'], live)
        if targets and sources:
            out.append({'src': sources, 'dst': targets, 'proto': rule['proto'], 'ports': [list(p) for p in rule['ports']]})
    return out


# ----- gateway subnets: a route only where the rules let traffic through -----
def _nets(values):
    return [ipaddress.ip_network(v) for v in values]


def _tidy(nets):
    """Unique networks without those another one holds, in address order."""
    unique = sorted(set(nets), key=lambda n: (n.prefixlen, n.network_address))
    kept = []
    for net in unique:
        if not any(net.subnet_of(k) for k in kept):
            kept.append(net)
    return [str(n) for n in sorted(kept, key=lambda n: (n.network_address, n.prefixlen))]


def _in_gateways(net, gateways):
    """The parts of net inside the gateway subnets (two CIDRs overlap only when one holds the other)."""
    return [net if net.subnet_of(g) else g for g in gateways if net.overlaps(g)]


def _held(f):
    """[(network, interface)] a node's own machine already routes: known only where facts carry
    its routing table (hostRoutes, the controller's machine), with its interface networks."""
    if f.get('hostRoutes') is None:
        return []
    out = [(ipaddress.ip_network(r['net']), r['dev']) for r in f['hostRoutes']]
    for raw in f.get('localPrefixes') or []:
        with contextlib.suppress(TypeError, ValueError):
            out.append((ipaddress.ip_network(raw, strict=False), ''))
    return out


def route_conflicts(doc, facts):
    """{serverId: [(route, held, interface)]}: gateway routes left off a node because its machine
    already routes part of them elsewhere (a VPN, a container bridge); installing them would take
    that traffic into the mesh."""
    return gateway_routes(doc, facts, conflicts=True)


def routes_and_conflicts(doc, facts):
    """(gateway_routes, route_conflicts) worked out once, for a caller that needs both."""
    return gateway_routes(doc, facts, conflicts=None)


def gateway_routes(doc, facts=None, *, conflicts=False):
    """{serverId: [CIDR]} for every member not being removed: the gateway subnets it gets a
    route to. A node is a rule's source when the rule's src names it (members, tags, a prefix
    holding its mesh address) or, for a gateway, holds part of its own subnets: it then gets
    the rule's dst.subnets. A node is a rule's destination when the dst names it or, for a
    gateway, takes in part of its own subnets: it then gets the rule's src.prefixes inside
    gateway subnets, to answer them.
    Subnets a gateway announces itself are never in its own list, nor (with facts) any its machine
    already routes elsewhere; conflicts=True returns those instead, as route_conflicts, and
    conflicts=None both, as routes_and_conflicts."""
    live = {m['serverId']: m for m in _live(doc)}
    own = {sid: _nets((m['gateway'] or {}).get('subnets', [])) for sid, m in live.items()}
    gateways = [net for nets in own.values() for net in nets]

    rules = [(rule, _nets(side_nets(rule['src'], 'prefixes', live)), _nets(side_nets(rule['dst'], 'subnets', live))) for rule in doc['rules']]
    out = {}
    for sid, m in live.items():
        mine = own[sid]
        address = ipaddress.ip_address(m['address']) if m['address'] else None
        routes = []
        for rule, prefixes, subnets in rules:
            src, dst = rule['src'], rule['dst']
            if (named(src, m) or any(address and address in p for p in prefixes)
                    or any(p.overlaps(g) for p in prefixes for g in mine)):
                routes += subnets
            if named(dst, m) or any(s.overlaps(g) for s in subnets for g in mine):
                routes += [part for p in prefixes for part in _in_gateways(p, gateways)]
        out[sid] = _tidy(r for r in routes if not any(r.subnet_of(g) for g in mine))
    skipped = {}
    for sid in out:
        held = _held((facts or {}).get(sid) or {})
        if held:
            clash = {r: [(n, dev) for n, dev in held if n.version == 4 and ipaddress.ip_network(r).overlaps(n)] for r in out[sid]}
            skipped[sid] = [(r, str(n), dev) for r, found in clash.items() for n, dev in found[:1]]
            out[sid] = [r for r in out[sid] if not clash[r]]
    skipped = {sid: v for sid, v in skipped.items() if v}
    return (out, skipped) if conflicts is None else skipped if conflicts else out


def exports(doc, routes=None):
    """[{gateway, subnet, nodes}]: for every announced subnet, the members that get a route
    into it, in member order."""
    routes = gateway_routes(doc) if routes is None else routes
    live = _live(doc)
    out = []
    for g in live:
        for subnet in (g['gateway'] or {}).get('subnets', []):
            net = ipaddress.ip_network(subnet)
            nodes = [m['serverId'] for m in live if m['serverId'] != g['serverId']
                     and any(ipaddress.ip_network(r).overlaps(net) for r in routes.get(m['serverId'], []))]
            out.append({'gateway': g['serverId'], 'subnet': subnet, 'nodes': nodes})
    return out


# ----- a member next to its site relay: the relay's LAN address -----
def _lan_addresses(facts, sid):
    out = []
    for raw in (facts.get(sid) or {}).get('localAddresses') or []:
        try:
            iface = ipaddress.ip_interface(raw)
        except (TypeError, ValueError):
            continue
        if is_lan(iface.ip) and not any(iface.ip in b for b in BRIDGE_NETS):
            out.append(iface)
    return out


def lan_endpoint(member, relay, facts):
    """udp://<relay's LAN address>:<its listen.udp> when the member has an address on one of
    the relay's networks; the relay's first such address in the order it reported them."""
    port = relay['listen']['udp']
    mine = _lan_addresses(facts, member['serverId'])
    if not port or not mine:
        return ''
    for theirs in _lan_addresses(facts, relay['serverId']):
        if any(m.ip != theirs.ip and m.network.overlaps(theirs.network) for m in mine):
            return endpoint('udp', str(theirs.ip), port)
    return ''


def punched(pair, kind):
    return bool(pair.get('punch')) if kind == 'relay' else pair['transport'] == 'udp'


def _link(pair, me, peer, kind, live, facts):
    dial = (pair['dialer'] == 'a') == (pair['a'] == me)
    link = {'peer': peer, 'kind': kind, 'dial': dial}
    if punched(pair, kind):
        # Both ends punch; each gets the other's address only where one is known to reach it.
        link['punch'] = True
        target_endpoint = punch_endpoint(live[peer], facts)
        if target_endpoint:
            link['endpoint'] = target_endpoint
    elif dial:
        target = dial_target(live[peer], pair['transport'], facts)
        if target:
            link['endpoint'] = target[0]
            if pair['transport'] == 'tls':
                link['tlsPins'] = target[1]
        else:
            # Kept as a link (next hops and bias name it) that waits instead of dialing,
            # until a reissue with the address.
            link['dial'] = False
    return link


def _key32(value):
    try:
        return isinstance(value, str) and len(base64.b64decode(value.strip(), validate=True)) == 32
    except (ValueError, binascii.Error):
        return False


def key_bytes(value):
    """What a base64 key stands for, as nodes decode it: spellings that differ only in the unused
    bits of the last character are one key to them. Anything that does not decode stands for itself."""
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (AttributeError, ValueError, binascii.Error):
        return value


def keyless(members, facts):
    """The members no other config may name: without a WireGuard, control, signing and end-to-end
    key of 32 bytes each (the last two not reported yet: an old data plane, one fallen back; one
    stored before keys were held to that), or with an end-to-end key a member
    before it also has, or that is a WireGuard key. A data plane refuses the whole config for any
    such entry, and takes nothing such a node says. Stored keys are unique per owner
    (mesh._store_security_keys), so only records from before that repeat one; the first member
    keeps it, as a member copying another's must not take that one out with it."""
    of = {m['serverId']: facts.get(m['serverId']) or {} for m in members}
    out = {sid for sid, f in of.items() if not all(_key32(f.get(k)) for k in ('publicKey', 'controlKey', 'signKey', 'e2eKey'))}
    wireguard = {key_bytes(f.get('publicKey')) for f in of.values()}
    held = set()
    for sid, f in of.items():
        if sid in out:
            continue
        e2e = key_bytes(f['e2eKey'])
        if e2e in held or e2e in wireguard:
            out.add(sid)
        held.add(e2e)
    return out


def _without(doc, ids):
    """doc as if the members ids were removed, with everything that points at them."""
    from app.servers import mesh_model  # it imports this module
    out = copy.deepcopy(doc)
    out['members'] = [m for m in out['members'] if m['serverId'] not in ids]
    for sid in ids:
        mesh_model.strip(out, sid)
    return out


def build(doc, facts, network_id, revision):
    """{serverId: config} for every member not being removed. A member whose keys are not in
    yet (an old data plane, one that fell back after a failed update, one not reported yet) is
    left out of every other config, as if removed: only it is cut off, and every other node
    gets a config it accepts. Its own config names only the members with keys; nobody
    answers it until a reissue with its keys."""
    left = sorted(keyless(_live(doc), facts))
    if not left:
        return _build(doc, facts, network_id, revision)
    configs = _build(_without(doc, left), facts, network_id, revision)
    for sid in left:
        configs[sid] = _build(_without(doc, [k for k in left if k != sid]), facts, network_id, revision)[sid]
    return configs


def _build(doc, facts, network_id, revision):
    s = doc['settings']
    live = {m['serverId']: m for m in _live(doc)}
    relays = sorted(sid for sid, m in live.items() if m['role'] == 'relay')
    pairs = relay_pairs(doc, facts)
    hops = next_hops(relays, pairs)
    network = {'id': network_id, 'prefix': s['prefix'], 'controlPrefix': s['controlPrefix'], 'mtu': s['mtu']}
    health = {**HEALTH, **s['health']}
    reachability = {'stun': list((s.get('reachability') or {}).get('stun', []))}

    def relays_of(m):
        if m['role'] != 'member':
            return []
        return list(dict.fromkeys(c['relay'] for c in m['entry']['candidates'] if live.get(c['relay'], {}).get('role') == 'relay'))

    def name(sid):
        return (facts.get(sid) or {}).get('name') or sid
    routes = gateway_routes(doc, facts)
    nodes = {sid: {'id': sid, 'name': name(sid), 'role': m['role'], 'address': m['address'], 'controlAddress': m['controlAddress'],
                   'publicKey': (facts.get(sid) or {}).get('publicKey', ''), 'controlKey': (facts.get(sid) or {}).get('controlKey', ''),
                   # Others accept what it signs, and reach it end to end, only with these (build
                   # leaves out a member without them).
                   'signKey': (facts.get(sid) or {}).get('signKey') or '', 'e2eKey': (facts.get(sid) or {}).get('e2eKey') or '',
                   'subnets': list((m['gateway'] or {}).get('subnets', [])), 'relays': relays_of(m)}
             for sid, m in live.items()}
    # Without a common one (the draft check reports it) the newest is written, and delivery
    # skips the members that cannot read it.
    schema = config_schema(doc, facts) or SCHEMA[1]
    # Every relay link of the mesh, each pair once: a relay's router takes no other link from
    # what other relays advertise.
    relay_links = sorted({tuple(sorted((p['a'], p['b']))) for p in pairs if p['a'] in live and p['b'] in live and p['a'] != p['b']})
    configs = {}
    for sid, m in live.items():
        relay = m['role'] == 'relay'
        links, bias = [], {}
        for pair in (pairs if relay else doc['directLinks']):
            if sid in (pair['a'], pair['b']) and pair['a'] != pair['b']:
                peer = pair['b'] if pair['a'] == sid else pair['a']
                if peer in live:
                    links.append(_link(pair, sid, peer, 'relay' if relay else 'direct', live, facts))
                    bias[peer] = pair.get('bias', 0)
        entry = None
        if not relay:
            candidates = []
            for c in m['entry']['candidates']:
                target = live.get(c['relay'])
                if not target or target['role'] != 'relay':
                    continue
                if is_site(target):
                    # Punched through the member's other candidates; an endpoint only where one is known,
                    # the relay's LAN address first when the member sits on the same network.
                    candidate = {'id': c['id'], 'relay': c['relay'], 'punch': True}
                    target_endpoint = lan_endpoint(m, target, facts) or punch_endpoint(target, facts)
                    if target_endpoint:
                        candidate['endpoint'] = target_endpoint
                    candidates.append(candidate)
                    continue
                dialed = dial_target(target, c['transport'], facts)
                if not dialed:
                    # Left out until a reissue with the address; the member's other candidates stay.
                    continue
                candidate = {'id': c['id'], 'relay': c['relay'], 'endpoint': dialed[0]}
                if c['transport'] == 'tls':
                    candidate['tlsPins'] = dialed[1]
                candidates.append(candidate)
            entry = {'mode': m['entry']['mode'], 'candidates': candidates, 'autoReturn': m['entry']['autoReturn']}
        configs[sid] = {
            'schema': schema, 'revision': revision, 'network': network,
            'self': {'id': sid, 'name': name(sid), 'role': m['role'], 'address': m['address'], 'controlAddress': m['controlAddress'],
                     'listen': dict(m['listen']), 'gateway': dict(m['gateway']) if m['gateway'] else None,
                     'uploadLimitMbps': m['uploadLimitMbps'], 'resources': m['role'],
                     # Gateway subnets this node routes to: only those the rules let it reach or answer.
                     'gatewayRoutes': routes[sid]},
            'nodes': [nodes[other] for other in sorted(nodes) if other != sid],
            'links': sorted(links, key=lambda link: link['peer']),
            'entry': entry,
            'nextHops': dict(sorted(hops[sid].items())) if relay else {},
            'rules': node_rules(doc, sid, facts),
            'health': health,
            # Every node checks its reachability: entry relays, or for a relay its linked ones, asked first.
            'reflectors': reflectors(live.values(), facts, sorted(bias) if relay else [c['relay'] for c in m['entry']['candidates']], sid),
            'reachability': reachability,
        }
        if relay:
            # nextHops stay the start and the static mode; bias shifts the measured cost of each link.
            configs[sid]['routing'] = {'mode': 'dynamic', 'bias': dict(sorted(bias.items()))}
            configs[sid]['relayLinks'] = [list(p) for p in relay_links]
    return configs
