"""Fixed-target management transport to an instance, and the HTTP path the
controller uses for instances and the access gateway alike: through the
server's node to its loopback when the node can, else to the recorded address."""
from __future__ import annotations

import json

import aiohttp
from yarl import URL

from app.instances.contracts import InstanceError, relay_path
from app.instances.security import Scrubber, secret_values

MAX_RESPONSE = 2 * 1024 * 1024
MAX_DEPTH = 64
# Reported text is stored and returned to the model; its sender decides its size.
TEXT_LIMIT = 200
ISSUES_LIMIT = 20


class TransportError(Exception):
    """'unreachable' or 'too_large'; callers name it for their peer."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


async def fetch(service, route, method, path, *, headers=None, body=None, timeout=20):
    """(status, raw body) of one request. route {'url': origin} goes straight over HTTP;
    {'owner', 'serverId', 'port'} has that server's node ask 127.0.0.1:port and return the
    reply over its encrypted channel. Redirects are never followed."""
    data = None if body is None else json.dumps(body).encode()
    headers = {**(headers or {}), **({'Content-Type': 'application/json'} if data is not None else {})}
    if 'url' not in route:
        try:
            return await service.servers.local_http(route['owner'], route['serverId'], method, route['port'], path,
                                                    headers=headers, body=data or b'', timeout=timeout)
        except InstanceError as exc:
            raise TransportError('too_large' if exc.error == 'response_too_large' else 'unreachable') from None
    try:
        # Never trust proxy env or ambient cookies; no redirect receives a key.
        async with service.http.request(method, URL(route['url'] + path, encoded=True), data=data, headers=headers,
                                        allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise TransportError('too_large')
            return response.status, bytes(raw)
    except (TimeoutError, aiohttp.ClientError):
        raise TransportError('unreachable') from None


def loads(raw):
    """JSON from an instance or the gateway, nested at most MAX_DEPTH deep: everything that
    later walks it recursively (scrubbing, storage, tool results) stays bounded."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError('invalid_json') from None
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            if depth > MAX_DEPTH:
                raise ValueError('json_too_deep')
            stack.extend((v, depth + 1) for v in (item.values() if isinstance(item, dict) else item))
    return value


async def route(service, row, item):
    """A remote instance is reached on its server's loopback through a node that can;
    the local one and SSH-only servers at the recorded address. One placed on loopback
    (item.network) only ever through the node: its address means nothing from here."""
    credentials = service.vault.open(row['protected']) if row.get('protected') else {}
    if (credentials.get('form') or {}).get('managementUrl'):
        return {'url': item['managementUrl']}
    if item.get('serverId') not in (None, '', 'local') and item.get('port') and (
            item.get('network') or await service.servers.http_ready(row['owner'], item['serverId'])):
        return {'owner': row['owner'], 'serverId': item['serverId'], 'port': item['port']}
    return {'url': item['managementUrl']}


async def request(service, row, method, path, body=None, *, raw_path=False):
    item = json.loads(row['public_json'])
    credentials = service.vault.open(row['protected'])
    if not raw_path:
        path = relay_path(method, path)
    headers = {'Authorization': 'Bearer ' + credentials['managementKey']}
    # relay_path produces a canonical encoded path. Preserve encoded slashes in
    # opaque model IDs: aiohttp routes them as one segment, then decodes match_info.
    try:
        status, raw = await fetch(service, await route(service, row, item), method, path, headers=headers, body=body)
        if 300 <= status < 400:
            raise InstanceError('child_redirect_rejected', 502)
        try:
            data = loads(raw)
        except ValueError:
            raise InstanceError('child_invalid_response', 502) from None
        # Keys must not leak even if a malicious child reflects the bearer.
        return status, Scrubber(secret_values(credentials))(data)
    except TransportError as exc:
        raise InstanceError('child_response_too_large' if exc.kind == 'too_large' else 'child_unreachable', 502) from None
    except InstanceError:
        raise
    except Exception:
        # Whatever a child sends, the operation waiting on it must still finish.
        raise InstanceError('child_invalid_response', 502) from None


def _text(value, limit=TEXT_LIMIT):
    return value[:limit] if isinstance(value, str) else ''


def clean_health(health):
    health = health if isinstance(health, dict) else {}
    issues = health.get('issues') if isinstance(health.get('issues'), list) else []
    return {'status': _text(health.get('status'), 80) or 'unknown',
            'issues': [_text(issue) for issue in issues[:ISSUES_LIMIT] if isinstance(issue, str)]}


def clean_status(child):
    """The child status a gateway probe relays, cut to the fields and sizes kept."""
    child = child if isinstance(child, dict) else {}
    persistence = child.get('persistence') if isinstance(child.get('persistence'), dict) else None
    return {'ok': child['ok'] if isinstance(child.get('ok'), bool) else None,
            'instanceId': _text(child.get('instanceId'), 128) or None,
            'ready': child['ready'] if isinstance(child.get('ready'), bool) else None,
            'health': clean_health(child['health']) if isinstance(child.get('health'), dict) else None,
            'persistence': None if persistence is None else {
                'root': _text(persistence.get('root'), 512), 'mounted': persistence.get('mounted') is True,
                **{k: _text(persistence.get(k), 512) for k in ('dbPath', 'workspacePath')}},
            'version': _text(child.get('version'), 80) or None}


def failed_check(error):
    """A probe result for a probe that could not run at all."""
    return {'identity': False, 'ready': False, 'persistence': False, 'health': {'status': 'unknown', 'issues': []},
            'publicRoute': False, 'gateway': {'ok': False, 'error': error}, 'error': error, 'ok': False}


def child_evidence(data, identity):
    data = data if isinstance(data, dict) else {}
    correct = data.get('ok') is True and data.get('instanceId') == identity
    persistence = data.get('persistence') if isinstance(data.get('persistence'), dict) else {}
    return {'identity': correct, 'ready': correct and data.get('ready') is True,
            'health': clean_health(data.get('health')) if correct else {'status': 'unknown', 'issues': []},
            'persistence': bool(correct and persistence.get('root') == '/workspace' and persistence.get('mounted') is True
                and all(str(persistence.get(k, '')).startswith('/workspace/') and '..' not in str(persistence.get(k, '')).split('/') for k in ('dbPath', 'workspacePath')))}


async def probe(service, row):
    """Instance identity/health directly, plus the same request through the gateway."""
    import asyncio

    async def direct():
        try:
            status, data = await request(service, row, 'GET', '/api/managed/v1/status', raw_path=True)
            return {'httpStatus': status, 'error': '', **child_evidence(data if status == 200 else None, row['id'])}
        except InstanceError as exc:
            return {'httpStatus': None, 'error': exc.error, **child_evidence(None, row['id'])}

    async def routed():
        try:
            return await service.gateway.probe(row)
        except Exception:
            return {'ok': False, 'error': 'gateway_invalid_response'}
    observed, gateway = await asyncio.gather(direct(), routed())
    check = {k: observed[k] for k in ('identity', 'ready', 'persistence', 'health')}
    check.update(publicRoute=bool(gateway['ok']), gateway=gateway, error=observed['error'] or gateway.get('error', ''))
    if not check['error'] and not all(check[k] for k in ('identity', 'ready', 'persistence')):
        check['error'] = 'identity_readiness_or_persistence_unverified'
    check['ok'] = all(check[k] for k in ('identity', 'ready', 'persistence', 'publicRoute')) and check['health']['status'] == 'healthy'
    return Scrubber(secret_values(service.vault.open(row['protected'])))(check)
