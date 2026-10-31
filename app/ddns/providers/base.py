"""DNS provider adapters: one subclass per service, registered by type.

Adapters only talk to the provider's API. Which value a record should hold,
when to write and how to retry belong to the DDNS service.
"""
from __future__ import annotations

import dataclasses
import email.utils
import time

import httpx

USER_AGENT = 'OpenBear-DDNS'
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

# Errors the service must not retry on its own: only a credential or
# configuration change, or a manual sync, can fix them.
PERMANENT = frozenset({'auth_failed', 'forbidden', 'zone_not_found', 'bad_request', 'unsupported'})
CODES = PERMANENT | {'rate_limited', 'network', 'server_error', 'invalid_response', 'record_conflict'}


class ProviderError(Exception):
    def __init__(self, code, message='', *, retry_after=None, status=None):
        assert code in CODES, code
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retry_after = retry_after
        self.status = status

    @property
    def permanent(self):
        return self.code in PERMANENT


@dataclasses.dataclass(frozen=True)
class Field:
    """A credential field, or a per-record option the provider understands."""
    key: str
    label: str
    kind: str = 'text'  # text | password | textarea | select | bool | number
    hint: str = ''
    options: tuple = ()
    optional: bool = False
    default: object = None

    def public(self):
        out = {'key': self.key, 'label': self.label, 'kind': self.kind}
        if self.hint:
            out['hint'] = self.hint
        if self.options:
            out['options'] = list(self.options)
        if self.optional:
            out['optional'] = True
        if self.default is not None:
            out['default'] = self.default
        return out


@dataclasses.dataclass(frozen=True)
class Caps:
    zones: bool = True       # can list the zones a credential manages
    read: bool = True        # can read a record's current value; update-only APIs cannot
    by_id: bool = True       # records carry an id usable for update and delete
    ttl_min: int = 60
    ttl_max: int = 86400
    ttl_auto: bool = False   # has an "automatic" TTL (Cloudflare's 1)
    types: tuple = ('A', 'AAAA')
    delete: bool = True
    multi: bool = True       # several records may share one name and type
    whole_zone: bool = False  # every write rewrites the whole zone: writes to one zone must not overlap

    def public(self):
        return {'zones': self.zones, 'read': self.read, 'byId': self.by_id, 'ttlMin': self.ttl_min, 'ttlMax': self.ttl_max,
                'ttlAuto': self.ttl_auto, 'types': list(self.types), 'delete': self.delete, 'multi': self.multi}


@dataclasses.dataclass
class Record:
    id: str      # provider-side id; '' when the provider has none
    name: str    # fully qualified, ASCII (punycode), lower case, no trailing dot
    type: str    # A | AAAA
    value: str
    ttl: int | None = None
    extra: dict = dataclasses.field(default_factory=dict)  # proxied, line, ...

    def public(self):
        return {'id': self.id, 'name': self.name, 'type': self.type, 'value': self.value, 'ttl': self.ttl, 'extra': dict(self.extra)}


class Provider:
    type = ''
    name = ''
    tier = 'A'          # A: token | B: request signing | C: protocol or generic
    fields: tuple = ()  # credential fields
    options: tuple = ()  # per-record options
    caps = Caps()
    docs = ''           # how to create a least-privilege credential
    endpoint = ''       # default API base; settings['endpoint'] overrides it

    def __init__(self, creds, settings=None, *, client=None):
        self.creds = {k: v for k, v in (creds or {}).items() if v not in (None, '')}
        self.settings = dict(settings or {})
        self.base = (self.settings.get('endpoint') or self.endpoint).rstrip('/')
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(timeout=TIMEOUT, proxy=self.settings.get('proxy') or None,
                                                  trust_env=False, headers={'User-Agent': USER_AGENT})
        missing = [f.key for f in self.fields if not f.optional and f.kind != 'bool' and not self.creds.get(f.key)]
        if missing:
            raise ProviderError('bad_request', 'missing credential: ' + ', '.join(missing))

    async def aclose(self):
        if self._own_client:
            await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    # ----- what each adapter implements -----
    async def zones(self):
        """Zones (apex names, ASCII, lower case) the credential can manage."""
        raise ProviderError('unsupported', 'listing zones is not supported')

    async def check(self):
        """Prove the credential works: {'zones': [...], 'writable': True | False | None}."""
        return {'zones': await self.zones(), 'writable': None}

    async def records(self, zone, name, rtype):
        """Records with exactly this name and type."""
        raise NotImplementedError

    async def create(self, zone, name, rtype, value, ttl, opts):
        raise NotImplementedError

    async def update(self, zone, record, value, ttl, opts):
        raise NotImplementedError

    async def delete(self, zone, record):
        raise ProviderError('unsupported', 'deleting records is not supported')

    # ----- helpers -----
    @staticmethod
    def relative(zone, name, apex='@'):
        """The record name relative to its zone: 'www' for www.example.com, apex for the zone itself."""
        zone, name = zone.lower().rstrip('.'), name.lower().rstrip('.')
        if name == zone:
            return apex
        if not name.endswith('.' + zone):
            raise ProviderError('bad_request', f'{name} is not inside {zone}')
        return name[:-len(zone) - 1]

    @staticmethod
    def fqdn(zone, relative):
        relative = (relative or '').lower().rstrip('.')
        zone = zone.lower().rstrip('.')
        if relative in ('', '@'):
            return zone
        return relative if relative == zone or relative.endswith('.' + zone) else relative + '.' + zone

    def on_line(self, found, opts):
        """The records of found on the line opts choose (the provider's default line when none):
        a name's records on other lines (电信, 联通...) are separate records."""
        field = next((f for f in self.options if f.key == 'line'), None)
        if field is None:
            return list(found)
        line = (opts or {}).get('line') or field.default
        return [r for r in found if (r.extra.get('line') or field.default) == line]

    def ttl_for(self, ttl):
        """The TTL to send: the record's own, else the account default, clamped to the provider's range."""
        value = ttl if ttl else self.settings.get('ttl')
        if not value:
            return None
        return max(self.caps.ttl_min, min(self.caps.ttl_max, int(value)))

    async def request(self, method, url, *, ok=(200, 201, 202, 204), **kwargs):
        """Send one API request; transport and HTTP failures become ProviderError."""
        if not url.startswith(('http://', 'https://')):
            url = self.base + url
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderError('network', 'timeout: ' + type(exc).__name__) from None
        except httpx.HTTPError as exc:
            # Not cut here: the service masks credentials before it shortens a message.
            raise ProviderError('network', f'{type(exc).__name__}: {exc}') from None
        if response.status_code in ok:
            return response
        raise self.http_error(response)

    def http_error(self, response, message=''):
        """message: what the provider's error fields say; the body itself is never repeated (an
        endpoint behind a proxy or mirror may answer anything)."""
        status = response.status_code
        detail = message or f'HTTP {status}'
        if status == 401:
            return ProviderError('auth_failed', detail, status=status)
        if status == 403:
            return ProviderError('forbidden', detail, status=status)
        if status == 429:
            return ProviderError('rate_limited', detail, status=status, retry_after=retry_after(response))
        if status >= 500:
            return ProviderError('server_error', detail, status=status, retry_after=retry_after(response))
        return ProviderError('bad_request', detail, status=status)

    @staticmethod
    def json(response):
        try:
            return response.json()
        except ValueError:
            raise ProviderError('invalid_response', f'not JSON (HTTP {response.status_code})') from None

    @classmethod
    def public(cls):
        return {'type': cls.type, 'name': cls.name, 'tier': cls.tier, 'fields': [f.public() for f in cls.fields],
                'options': [f.public() for f in cls.options], 'caps': cls.caps.public(), 'docs': cls.docs}


def retry_after(response):
    value = response.headers.get('Retry-After', '').strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


REGISTRY = {}


def register(cls):
    assert cls.type and cls.type not in REGISTRY, cls.type
    REGISTRY[cls.type] = cls
    return cls
