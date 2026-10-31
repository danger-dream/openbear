"""What nodes report about their data plane (status.json plus new events), the
summary the page shows, and the conditions that raise a server notification."""
from __future__ import annotations

import contextlib
import ipaddress
from collections import deque

from app.servers.reminders import quoted

EVENTS_KEPT = 100
# Resident memory the data plane should stay under (plan §13); above it a notification goes out.
MEMORY_LIMIT = {'member': 160 * 1024 ** 2, 'relay': 256 * 1024 ** 2}
NO_ENTRY_GRACE_MS = 60_000
SWITCH_WINDOW_MS = 5 * 60_000
SWITCHES_ALERTED = 3
FLAP_QUIET_MS = 30 * 60_000
# One kind of alert for one node at most this often: a condition that comes and goes starts a new
# episode each time, and would otherwise send one each time.
ALERT_COOLDOWN_MS = 30 * 60_000
PEER_KINDS = ('entry', 'relay', 'direct', 'member')
COUNTERS = ('delivered', 'forwarded', 'droppedAcl', 'droppedNotRelay', 'droppedTtl', 'droppedNoRoute', 'droppedQueue', 'droppedBad', 'droppedPlain')
# A node's end-to-end sessions: what it counts besides the sessions in use and their limit.
E2E_COUNTS = ('created', 'evicted', 'sent', 'received', 'droppedExhausted', 'droppedQueue', 'droppedSource', 'droppedBad')
HOPS_KEPT = 64
RESOURCES = ('rssBytes', 'heapBytes', 'queueBytes', 'conns', 'goroutines')
ROUTES_KEPT = 64
DIRECT_STATES = ('direct', 'punching', 'relayed')
DIRECT_VIA = ('lan', 'public', 'configured', 'routed', '')
VERDICTS = ('public', 'punchable', 'symmetric', 'no_udp', 'unknown')
MAPPINGS = ('stable', 'varies', 'unknown')
FILTERINGS = ('open', 'address', 'port', 'unknown')
OBSERVER_KINDS = ('relay', 'stun')
OBSERVERS_KEPT = 32
CANDIDATE_STATES = ('active', 'ready', 'failing', 'punching', 'untested')
CANDIDATES_KEPT = 16
LINE_ROLES = ('active', 'standby', 'cold')
ROUTING_MODES = ('dynamic', 'static')
LINK_STATES = ('up', 'down', 'suppressed')
PATH_KEPT = 16
ERRORS_TEXT = {'timeout': '超时', 'refused': '被拒绝', 'tls pin mismatch': 'TLS 证书不匹配', 'no handshake': '握手无响应',
               'unreachable': '不可达', 'punch timeout': '打洞超时'}
# Memory near its limit from this share on.
MEMORY_NEAR = 0.8


def _num(value, cap=2 ** 63):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < cap else 0


def _text(value, limit):
    return str(value)[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def _prefixes(raw):
    out = []
    for value in (raw if isinstance(raw, list) else [])[:ROUTES_KEPT]:
        with contextlib.suppress(TypeError, ValueError):
            network = ipaddress.ip_network(value if isinstance(value, str) else '', strict=False)
            if network.version == 4:
                out.append(str(network))
    return list(dict.fromkeys(out))


def _signed(value, cap=1e7):
    """A finite number within ±cap, one decimal; None otherwise (unknown or infinite)."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and -cap < value < cap
    return round(float(value), 1) if ok else None


def _ms(value):
    """A round trip in milliseconds; None when not measured."""
    return round(float(_num(value, 1e7)), 1) if value is not None else None


def _reachability(raw):
    if not isinstance(raw, dict):
        return None
    observers = []
    for o in (raw.get('observers') if isinstance(raw.get('observers'), list) else [])[:OBSERVERS_KEPT]:
        if isinstance(o, dict):
            observers.append({'id': _text(o.get('id'), 128), 'kind': o.get('kind') if o.get('kind') in OBSERVER_KINDS else '',
                              'addr': _text(o.get('addr'), 64), 'ok': o.get('ok') is True})
    return {'checkedAt': int(_num(raw.get('checkedAt'))), 'verdict': raw.get('verdict') if raw.get('verdict') in VERDICTS else 'unknown',
            'publicAddr': _text(raw.get('publicAddr'), 64), 'consistent': raw.get('consistent') is not False,
            'mapping': raw.get('mapping') if raw.get('mapping') in MAPPINGS else 'unknown',
            'filtering': raw.get('filtering') if raw.get('filtering') in FILTERINGS else 'unknown', 'observers': observers}


def public_ip(status):
    """The IP others may aim at before punching: only when the node's reachability check says
    it is public, every observer saw the same address and it is a global one (as hub.clean_ip asks
    of a node's IPv4); '' otherwise. The node says what it is: a private one would have the others
    send punches and handshakes into their own networks."""
    reach = (status or {}).get('reachability') or {}
    if reach.get('verdict') != 'public' or reach.get('consistent') is not True:
        return ''
    ip = nat_ip(status)
    return ip if ip and ipaddress.ip_address(ip).is_global else ''


def nat_ip(status):
    """The IP the node's reachability check saw it at, whatever its verdict; '' without one.
    Two nodes seen at one IP sit behind the same NAT. Only compared, never sent to: public_ip
    is the one others aim at."""
    reach = (status or {}).get('reachability') or {}
    host = (reach.get('publicAddr') or '').rpartition(':')[0].strip('[]')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return ''
    return '' if ip.is_unspecified or ip.is_loopback or ip.is_multicast else str(ip)


def _rate(value):
    """A share 0..1, three decimals; None when not measured."""
    return round(float(value), 3) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 else None


def _redial(raw):
    """A dialed TCP/TLS path's re-dial state: its baseline round trip, backoff level, how many times
    it was re-dialed and when; None for a path the node does not re-dial."""
    if not isinstance(raw, dict):
        return None
    baseline = _ms(raw.get('baselineMs'))
    return {'baselineMs': baseline if baseline else None, 'level': int(_num(raw.get('level'), 10)), 'redials': int(_num(raw.get('redials'))),
            'lastAt': int(_num(raw.get('lastAt'))), 'nextAt': int(_num(raw.get('nextAt'))), 'degradedMinutes': int(_num(raw.get('degradedMinutes'), 1e6))}


def _candidates(raw):
    """Each entry candidate's health; one without a known state is dropped. rttMs and
    timeoutRate: what the node measured on it (a cold one by its checks only), None until it
    did or from a node that does not say; role active|standby|cold when the node says."""
    out = []
    for c in (raw if isinstance(raw, list) else [])[:CANDIDATES_KEPT]:
        if isinstance(c, dict) and c.get('state') in CANDIDATE_STATES:
            rtt = _ms(c.get('rttMs'))
            loss = _rate(c.get('timeoutRate', c.get('lossRate')))
            out.append({'id': _text(c.get('id'), 32), 'relay': _text(c.get('relay'), 128), 'transport': _text(c.get('transport'), 8),
                        'state': c['state'], 'since': int(_num(c.get('since'))), 'lastOkAt': int(_num(c.get('lastOkAt'))),
                        'lastError': _text(c.get('lastError'), 64), 'via': c.get('via') if c.get('via') in DIRECT_VIA else '',
                        'rttMs': rtt if rtt else None, 'timeoutRate': loss,
                        'role': c.get('role') if c.get('role') in LINE_ROLES else ''})
            redial = _redial(c.get('redial'))
            if redial:
                out[-1]['redial'] = redial
    return out


def _routing(raw):
    if not isinstance(raw, dict):
        return None
    links, routes = [], []
    for link in (raw.get('links') if isinstance(raw.get('links'), list) else [])[:300]:
        if isinstance(link, dict) and link.get('state') in LINK_STATES:
            links.append({'peer': _text(link.get('peer'), 128), 'state': link['state'], 'rttMs': _ms(link.get('rttMs')),
                          'jitterMs': round(float(_num(link.get('jitterMs'), 1e7)), 1), 'loss': round(float(_num(link.get('loss'), 1.000001)), 3),
                          'cost': _signed(link.get('cost')), 'penalty': int(_num(link.get('penalty'), 1e6))})
    for route in (raw.get('routes') if isinstance(raw.get('routes'), list) else [])[:300]:
        if isinstance(route, dict):
            path = route.get('path') if isinstance(route.get('path'), list) else []
            routes.append({'dest': _text(route.get('dest'), 128), 'nextHop': _text(route.get('nextHop'), 128), 'cost': _signed(route.get('cost')),
                           'path': [_text(n, 128) for n in path[:PATH_KEPT]], 'since': int(_num(route.get('since'))),
                           'switches': int(_num(route.get('switches')))})
    return {'mode': raw.get('mode') if raw.get('mode') in ROUTING_MODES else '', 'links': links, 'routes': routes}


def _e2e(raw):
    """A node's end-to-end sessions: in use, their limit, the inner MTU and its counts; None without."""
    if not isinstance(raw, dict):
        return None
    return {'sessions': int(_num(raw.get('sessions'), 1e6)), 'limit': int(_num(raw.get('limit'), 1e6)), 'innerMtu': int(_num(raw.get('innerMtu'), 1e5)),
            **{k: int(_num(raw.get(k))) for k in E2E_COUNTS}}


FIREWALL_KINDS = ('ufw', 'iptables', 'docker', 'firewalld', 'nftables')
FIREWALL_ISSUES = ('firewalld_zone', 'firewalld_failed', 'nftables_failed', 'docker_failed', 'iptables_legacy', 'iptables_nft',
                   'firewall_tool_missing')


def _host_firewall(raw):
    """'on' (the macOS application firewall), else the Linux host firewalls a node found, in its
    order (ufw or iptables, docker, firewalld, nftables), comma-separated; '' for none or unknown."""
    if raw == 'on':
        return 'on'
    kinds = [k for k in raw.split(',') if k in FIREWALL_KINDS] if isinstance(raw, str) else []
    return ','.join(dict.fromkeys(kinds))


def _firewall_issues(raw):
    """[{code, detail}]: what the host firewall may not let through (hostfw_linux.go); unknown codes dropped."""
    out = []
    for issue in (raw if isinstance(raw, list) else [])[:10]:
        if isinstance(issue, dict) and issue.get('code') in FIREWALL_ISSUES:
            out.append({'code': issue['code'], 'detail': _text(issue.get('detail'), 300)})
    return out


def _security(raw):
    """The signatures a node checked of what nodes say on another's behalf: verified, rejected and the
    rejected by the peer they came from; and the stored claims it dropped for want of valid ones. None without."""
    if not isinstance(raw, dict):
        return None
    signing = raw.get('signing') if isinstance(raw.get('signing'), dict) else {}
    claims = raw.get('claims') if isinstance(raw.get('claims'), dict) else {}
    hops = signing.get('byHop') if isinstance(signing.get('byHop'), dict) else {}
    by_hop = {_text(hop, 128): int(_num(n)) for hop, n in list(hops.items())[:HOPS_KEPT] if _num(n)}
    return {'signing': {'verified': int(_num(signing.get('verified'))), 'rejected': int(_num(signing.get('rejected'))), 'byHop': by_hop},
            'claims': {'expired': int(_num(claims.get('expired')))}}


def _direct(raw):
    """A punched direct link's state; without a known state there is nothing to show."""
    if not isinstance(raw, dict) or raw.get('state') not in DIRECT_STATES:
        return None
    error = raw.get('lastError')
    return {'state': raw['state'], 'via': raw.get('via') if raw.get('via') in DIRECT_VIA else '', 'since': int(_num(raw.get('since'))),
            'attempts': int(_num(raw.get('attempts'))), 'lastError': error[:64] if isinstance(error, str) else ''}


def clean_status(raw):
    """Node output is untrusted: known fields only, bounded."""
    raw = raw if isinstance(raw, dict) else {}
    peers = []
    for p in (raw.get('peers') if isinstance(raw.get('peers'), list) else [])[:300]:
        if not isinstance(p, dict):
            continue
        # active: for a relay peer, its session is up (it may forward without being the next hop for
        # anything); routed: some destination here has it as next hop.
        peers.append({'id': _text(p.get('id'), 128), 'kind': p.get('kind') if p.get('kind') in PEER_KINDS else '',
                      'active': p.get('active') is True, 'routed': p.get('routed') is True, 'transport': _text(p.get('transport'), 8), 'endpoint': _text(p.get('endpoint'), 120),
                      'localAddr': _text(p.get('localAddr'), 120), 'remoteAddr': _text(p.get('remoteAddr'), 120),
                      'lastHandshakeUnixNano': _text(p.get('lastHandshakeUnixNano'), 24),
                      'handshakes': int(_num(p.get('handshakes'))), 'reconnects': int(_num(p.get('reconnects'))), 'rxBytes': int(_num(p.get('rxBytes'))), 'txBytes': int(_num(p.get('txBytes'))),
                      'rttMs': _ms(p.get('rttMs')),
                      'timeoutRate': round(float(_num(p.get('timeoutRate'), 2)), 3)})
        direct = _direct(p.get('direct'))
        if direct:
            peers[-1]['direct'] = direct
        redial = _redial(p.get('redial'))
        if redial:
            peers[-1]['redial'] = redial
    entry = raw.get('entry') if isinstance(raw.get('entry'), dict) else None
    if entry is not None:
        entry = {'candidate': _text(entry.get('candidate'), 32), 'relay': _text(entry.get('relay'), 128), 'transport': _text(entry.get('transport'), 8),
                 'since': int(_num(entry.get('since'))), 'switches': int(_num(entry.get('switches'))), 'candidates': _candidates(entry.get('candidates'))}
    owners = {}
    for member, value in list((raw.get('owners') if isinstance(raw.get('owners'), dict) else {}).items())[:1000]:
        if isinstance(value, dict):
            owners[_text(member, 128)] = {'relay': _text(value.get('relay'), 128), 'seq': int(_num(value.get('seq'))), 'leaseMs': int(_num(value.get('leaseMs')))}
    counters = raw.get('counters') if isinstance(raw.get('counters'), dict) else {}
    resources = raw.get('resources') if isinstance(raw.get('resources'), dict) else {}
    return {'schema': int(_num(raw.get('schema'), 100)), 'pid': int(_num(raw.get('pid'))), 'version': _text(raw.get('version'), 40),
            'startedAt': int(_num(raw.get('startedAt'))), 'configRevision': int(_num(raw.get('configRevision'))),
            'appliedRevision': int(_num(raw.get('appliedRevision'))), 'error': _text(raw.get('error'), 500), 'ready': raw.get('ready') is True,
            'iface': _text(raw.get('iface'), 32), 'peers': peers, 'entry': entry, 'owners': owners,
            'counters': {k: int(_num(counters.get(k))) for k in COUNTERS},
            'resources': {**{k: int(_num(resources.get(k))) for k in RESOURCES}, 'cpuPercent': round(float(_num(resources.get('cpuPercent'), 1e5)), 1)},
            'skippedRoutes': _prefixes(raw.get('skippedRoutes')), 'hostFirewall': _host_firewall(raw.get('hostFirewall')),
            'hostFirewallIssues': _firewall_issues(raw.get('hostFirewallIssues')),
            'reachability': _reachability(raw.get('reachability')), 'routing': _routing(raw.get('routing')),
            'e2e': _e2e(raw.get('e2e')), 'security': _security(raw.get('security'))}


def clean_events(raw):
    out = []
    for e in (raw if isinstance(raw, list) else [])[-EVENTS_KEPT:]:
        if isinstance(e, dict) and type(e.get('seq')) is int and e['seq'] >= 0:
            out.append({'seq': e['seq'], 'at': int(_num(e.get('at'))), 'kind': _text(e.get('kind'), 40), 'detail': _text(e.get('detail'), 300)})
    return sorted(out, key=lambda e: e['seq'])


def merge_events(kept, new=()):
    """kept + new as a node's event list: an event repeating the one before it (same kind and
    detail) folds into it, keeping the newest seq and at, the first time as firstAt and how many
    times in all as repeat (absent on one that did not repeat). The newest EVENTS_KEPT are kept,
    counted after folding; a list stored before folding comes out folded."""
    out = []
    for e in [*kept, *new]:
        last = out[-1] if out else None
        if last and last['kind'] == e['kind'] and last['detail'] == e['detail']:
            out[-1] = {**e, 'firstAt': last.get('firstAt', last['at']), 'repeat': last.get('repeat', 1) + e.get('repeat', 1)}
        else:
            out.append(dict(e))
    return out[-EVENTS_KEPT:]


# ----- reading reports: shared by the overview's lines, paths and trends -----
def fresh_status(statuses, sid, now, fresh_ms):
    """sid's last cleaned status from statuses ({id: {'status', 'at'}} as kept for reports), or
    None when it has none or it is older than fresh_ms at now."""
    live = statuses.get(sid) or {}
    return live['status'] if live.get('status') and now - live.get('at', 0) <= fresh_ms else None


def records(status, other):
    """Every peer record status has for other, active ones first."""
    found = [p for p in (status or {}).get('peers', []) if p['id'] == other]
    return sorted(found, key=lambda p: not p['active'])


def direct_up(peer):
    """A peer record carrying a direct link that is up: punched (its direct state), or dialed (a
    direct record in use; it reports no punch state)."""
    return bool(peer) and ((peer.get('direct') or {}).get('state') == 'direct' or peer['kind'] == 'direct' and peer['active'] and not peer.get('direct'))


def entry_rtt(status, relay):
    """The round trip a member measured to one of its entry relays: its candidate in use there
    (state active), else its own peer record for that relay, an active one and an entry one first.
    A candidate not in use there speaks only for itself (another transport, a cold check), never for
    the relay. 0 is not a measurement: None when nothing was measured."""
    if not status:
        return None
    using = next((c for c in (status.get('entry') or {}).get('candidates', []) if c['relay'] == relay and c['state'] == 'active'), None)
    if using and using.get('rttMs'):
        return using['rttMs']
    peers = sorted((p for p in status['peers'] if p['id'] == relay and p['rttMs']), key=lambda p: (not p['active'], p['kind'] != 'entry'))
    return peers[0]['rttMs'] if peers else None


def links_rtt(status):
    """A relay's round trip as the page shows it: the shortest of its measured links to other relays
    (it does not measure the members it carries: they report 0), None without one."""
    rtts = [link['rttMs'] for link in ((status or {}).get('routing') or {}).get('links') or [] if link.get('rttMs')]
    return min(rtts) if rtts else None


def memory(status, role):
    """Resident memory against the limit for the role: level near from 80%, over at the limit."""
    limit = MEMORY_LIMIT['relay' if role == 'relay' else 'member']
    rss = status['resources']['rssBytes'] if status else 0
    return {'rssBytes': rss, 'limitBytes': limit, 'level': 'over' if rss >= limit else 'near' if rss >= limit * MEMORY_NEAR else ''}


def summary(status, role='member'):
    if not status:
        return {'entry': {}, 'ready': False, 'peers': 0, 'rttMs': None, 'memory': memory(None, role), 'direct': {},
                'reachability': None, 'entryHealth': [], 'routing': None}
    active = [p for p in status['peers'] if p['active']]
    entry = {k: v for k, v in (status['entry'] or {}).items() if k != 'candidates'}
    # A member: its entry as it measures it; a relay: its links to other relays. 0 is not a measurement.
    rtt = links_rtt(status) if role == 'relay' else entry_rtt(status, entry['relay']) if entry.get('relay') else None
    # Each punched direct peer's state, so the page needs no member detail to show it.
    direct = {}
    for p in status['peers']:
        if p.get('direct') and p['id'] not in direct:
            direct[p['id']] = {'state': p['direct']['state'], 'via': p['direct']['via'], 'rttMs': p['rttMs']}
    reach = status.get('reachability')
    routing = status.get('routing')
    return {'entry': entry, 'ready': status['ready'], 'peers': len(active), 'rttMs': rtt, 'memory': memory(status, role), 'direct': direct,
            'reachability': {k: reach[k] for k in ('verdict', 'publicAddr', 'consistent', 'filtering', 'checkedAt')} if reach else None,
            'entryHealth': list((status['entry'] or {}).get('candidates', [])) if role != 'relay' else [],
            'routing': {k: routing[k] for k in ('mode', 'links', 'routes')} if routing and role == 'relay' else None}


def node_state(*, cleanup, installing, online, configured, current, failed, status, missing=False):
    """installing|installed|ready|degraded|offline|failed|missing|cleanup, as the page shows a node.
    failed: the last delivery to it failed and it does not run the latest config; this
    wins over its own report, which only describes the config it kept. missing: the node
    says its mesh service is gone (uninstalled, or the node installed again); nothing
    goes to it until it is reinstalled, so this wins over being offline too."""
    if cleanup:
        return 'cleanup'
    if installing:
        return 'installing'
    if missing:
        return 'missing'
    if failed:
        return 'failed'
    if not online:
        return 'offline'
    if status and status['error']:
        return 'degraded'
    if not configured:
        return 'installed'
    return 'ready' if current and status and status['ready'] else 'degraded'


class Watch:
    """Alert bookkeeping for one node; observe returns (key, kind, text) for each new alert.
    Keys are stable per episode, so a notification is never queued twice."""

    def __init__(self):
        self.no_entry_since = 0
        self.no_entry_sent = False
        self.switches = deque(maxlen=SWITCHES_ALERTED * 4)
        self.flap_sent = 0
        self.over_since = 0
        self.over_sent = False
        self.standby_since = 0
        self.standby_sent = False
        self.inconsistent_since = 0
        self.inconsistent_sent = False
        self.last_sent = {}  # alert kind -> ms one was last sent

    def _due(self, kind, now):
        """Whether an alert of kind may go out now (ALERT_COOLDOWN_MS after the last); taken as sent
        when it may. One held back goes out later while its episode lasts."""
        if kind in self.last_sent and now - self.last_sent[kind] < ALERT_COOLDOWN_MS:
            return False
        self.last_sent[kind] = now
        return True

    def resume(self, status, role, now):
        """After a controller restart, from the status saved before it: the conditions alerted on
        their first sight (exit inconsistent, memory over the limit) that still held then count as
        already sent, so a restart alone does not send them again."""
        reach = status.get('reachability')
        if reach and not reach['consistent']:
            self.inconsistent_since, self.inconsistent_sent = now, True
        if status['resources']['rssBytes'] > MEMORY_LIMIT['relay' if role == 'relay' else 'member']:
            self.over_since, self.over_sent = now, True

    def observe(self, now, server_id, name, role, candidates, status, events, relay_name=None):
        alerts = []
        relay_name = relay_name or (lambda sid: sid)
        # Every standby candidate failing: the member would have nowhere to go if its entry broke.
        standby = [c for c in (status['entry'] or {}).get('candidates', []) if c['state'] != 'active']
        if role == 'member' and standby and all(c['state'] == 'failing' for c in standby):
            self.standby_since = self.standby_since or now
            if not self.standby_sent and now - self.standby_since >= NO_ENTRY_GRACE_MS and self._due('standby', now):
                self.standby_sent = True
                # The node's own words for an error it has no code for are quoted like a name: Telegram would link a URL in them.
                failing = '；'.join(f'{quoted(relay_name(c["relay"]))}备用：'
                                   f'{ERRORS_TEXT.get(c["lastError"]) or (quoted(c["lastError"]) if c["lastError"] else "失败")}' for c in standby)
                alerts.append((f'mesh_standby_unhealthy:{server_id}:{self.standby_since}', 'mesh',
                               f'组网：{quoted(name)}的备用入口都连不上（{failing}），当前入口断开时无法切换。'))
        else:
            self.standby_since, self.standby_sent = 0, False
        reach = status.get('reachability')
        if reach and not reach['consistent']:
            self.inconsistent_since = self.inconsistent_since or now
            if not self.inconsistent_sent and self._due('exit', now):
                self.inconsistent_sent = True
                alerts.append((f'mesh_exit_inconsistent:{server_id}:{self.inconsistent_since}', 'mesh',
                               f'组网：{quoted(name)}在不同观测点看到的出口地址不一致，出口可能被代理或多出口改写，打洞不会使用它的公网地址。'))
        elif reach:
            self.inconsistent_since, self.inconsistent_sent = 0, False
        entry = status['entry'] or {}
        connected = bool(entry.get('relay')) and any(p['active'] and p['kind'] == 'entry' for p in status['peers'])
        if role == 'member' and candidates and not connected:
            self.no_entry_since = self.no_entry_since or now
            if not self.no_entry_sent and now - self.no_entry_since >= NO_ENTRY_GRACE_MS and self._due('entry', now):
                self.no_entry_sent = True
                alerts.append((f'mesh-entry:{server_id}:{self.no_entry_since}', 'mesh',
                               f'组网：{quoted(name)}连不上任何入口，已持续 {(now - self.no_entry_since) // 60000} 分钟。'))
        else:
            self.no_entry_since, self.no_entry_sent = 0, False
        for e in events:
            if e['kind'] == 'entry.switch':
                self.switches.append(e['at'] or now)
        recent = [t for t in self.switches if now - t <= SWITCH_WINDOW_MS]
        if len(recent) >= SWITCHES_ALERTED and now - self.flap_sent >= FLAP_QUIET_MS:
            self.flap_sent = now
            alerts.append((f'mesh-flap:{server_id}:{recent[-SWITCHES_ALERTED]}', 'mesh',
                           f'组网：{quoted(name)}5 分钟内切换入口 {len(recent)} 次，线路可能不稳定。'))
        limit = MEMORY_LIMIT['relay' if role == 'relay' else 'member']
        rss = status['resources']['rssBytes']
        if rss > limit:
            self.over_since = self.over_since or now
            if not self.over_sent and self._due('memory', now):
                self.over_sent = True
                alerts.append((f'mesh-memory:{server_id}:{self.over_since}', 'mesh',
                               f'组网：{quoted(name)}的数据面内存 {rss // 1024 ** 2} MiB，超过上限 {limit // 1024 ** 2} MiB。'))
        elif rss < limit * 0.8:
            self.over_since, self.over_sent = 0, False
        return alerts
