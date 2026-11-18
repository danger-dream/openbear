"""Pure allocation of mesh-managed TCP listeners. No probing or writes.

User listeners, public mappings and custom global fast ports are pinned. Implicit fast
listeners and internal forward tunnels yield to those ports, deterministically. Existing
wire fields (fast.meshPorts, forward.port/fastPort) carry the result; no node extension.
"""
from __future__ import annotations


def _free(preferred, held):
    # Prefer the configured internal port; fallback never newly requires a privileged port.
    if preferred not in held:
        return preferred
    for n in range(65536 - 1024):
        port = (max(preferred, 1024) - 1024 + n) % (65536 - 1024) + 1024
        if port not in held:
            return port
    # No silent omission or invalid port 0. The validator reports actual capacity exhaustion.
    return None


def plan(doc, facts=None):
    from app.servers import mesh_fast, mesh_forward
    live = {m['serverId']: m for m in doc['members'] if not m.get('removing')}
    runs = mesh_forward.running(doc, live, facts)
    ports = [p for p in doc.get('fastPorts') or [] if p['serverId'] in live]
    hosts = {p['serverId'] for p in ports} | {f['target'] for f in runs if f['line'] == 'fast'}
    listeners = {sid for sid, m in live.items() if hosts and m.get('address') and
                 (facts is None or mesh_fast.supports_fast(facts, sid)) and (m['role'] == 'relay' or sid in hosts)}
    held = {}
    for sid, m in live.items():
        proxy = m.get('proxy') or {}
        held[sid] = {p for t, p in m['listen'].items() if p and t != 'fast'}
        held[sid].update(p['port'] for p in proxy.get('ports') or [])
        if proxy.get('exit'):
            held[sid].add(proxy['exit']['port'])
        # The legacy proxy tunnel is independently managed; do not move it here.
        held[sid].add((doc['settings'].get('proxy') or {}).get('port'))
        held[sid].update(p['port'] for p in ports if p['serverId'] == sid)
        held[sid].update(f['listen'] for f in runs if f['entry'] == sid and f['proto'] in ('tcp', 'any'))
        # Also avoid services a forward explicitly names on its target (including loopback).
        held[sid].update(f['port'] for f in runs if f['target'] == sid and f['proto'] in ('tcp', 'any'))
    default_fast = (doc['settings'].get('fast') or {}).get('port') or mesh_fast.DEFAULT_PORT
    fast, exhausted = {}, []
    for sid in sorted(listeners):
        m = live[sid]
        pinned = m['listen'].get('fast') or (m.get('publicPorts') or {}).get('fast') or default_fast != mesh_fast.DEFAULT_PORT
        preferred = m['listen'].get('fast') or default_fast
        port = preferred if pinned else _free(preferred, held[sid])
        if port is None:
            exhausted.append(sid)
            port = preferred
        fast[sid] = port
        held[sid].add(port)
    forward = {**mesh_forward.default_settings(), **(doc['settings'].get('forward') or {})}
    targets = {f['target'] for f in runs}
    # One port per tunnel kind across the mesh is required by the existing N-1 wire contract.
    # Only active tunnels reserve ports. Both kinds share the union so entries dial consistently.
    reserved = set().union(*(held[sid] for sid in targets)) if targets else set()
    active = {'port': any(f['line'] == 'mesh' for f in runs), 'fastPort': any(f['line'] == 'fast' for f in runs)}
    for key in ('port', 'fastPort'):
        if not active[key]:
            continue
        port = _free(forward[key], reserved)
        if port is None:
            exhausted.append(key)
        else:
            forward[key] = port
            reserved.add(port)
    # Even a fast-only config carries the plain port; node validation requires distinct ports.
    if active['fastPort'] and forward['port'] == forward['fastPort']:
        forward['port'] = _free(forward['port'], {forward['fastPort']})
    return {'fast': fast, 'forward': forward, 'exhausted': exhausted}
