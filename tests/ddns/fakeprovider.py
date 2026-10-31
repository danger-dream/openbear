"""In-memory DNS providers for the DDNS service tests: 'fake' reads and writes by record id,
'fakepush' can only update (like DuckDNS), 'fakename' has no record ids (like GoDaddy) and
'fakezone' rewrites the whole zone on every write (like Dynadot). Each token has its own zone
store, so a test can change records behind the service's back and queue errors."""
from __future__ import annotations

import asyncio
import itertools

from app.ddns.providers.base import REGISTRY, Caps, Field, Provider, ProviderError, Record

STORES = {}
_ids = itertools.count(1)


class Store:
    def __init__(self, zones=('example.com',)):
        self.zones = list(zones)
        self.records = {}   # id -> Record
        self.calls = []     # (operation, name, value)
        self.errors = []    # ProviderErrors raised by the next calls, one each
        self.writable = True

    def add(self, name, rtype, value, ttl=300, **extra):
        rid = f'rec{next(_ids)}'
        self.records[rid] = Record(rid, name, rtype, value, ttl, dict(extra))
        return rid

    def values(self, name, rtype='A'):
        return sorted(r.value for r in self.records.values() if r.name == name and r.type == rtype)

    def writes(self):
        return [c for c in self.calls if c[0] in ('create', 'update', 'delete')]

    def fail(self, code, times=1, **kwargs):
        self.errors += [ProviderError(code, f'{code} from fake', **kwargs) for _ in range(times)]

    def step(self, op, name='', value=''):
        self.calls.append((op, name, value))
        if self.errors:
            raise self.errors.pop(0)


def store(token):
    return STORES.setdefault(token, Store())


def reset():
    STORES.clear()


class FakeProvider(Provider):
    type = 'fake'
    name = 'Fake'
    fields = (Field('token', 'Token', 'password'),)
    options = (Field('proxied', '代理', 'bool', optional=True), Field('line', '线路', 'select', options=('default:默认', 'telecom:电信'), optional=True))
    caps = Caps(ttl_min=60, ttl_max=86400)

    def __init__(self, creds, settings=None, *, client=None):
        super().__init__(creds, settings, client=client or object())
        self.api = store(self.creds['token'])

    async def check(self):
        self.api.step('check')
        if self.creds['token'].startswith('bad'):
            raise ProviderError('auth_failed', f'token {self.creds["token"]} rejected')
        return {'zones': list(self.api.zones), 'writable': self.api.writable}

    async def records(self, zone, name, rtype):
        self.api.step('records', name)
        return [Record(r.id, r.name, r.type, r.value, r.ttl, dict(r.extra)) for r in self.api.records.values() if r.name == name and r.type == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        self.api.step('create', name, value)
        rid = self.api.add(name, rtype, value, self.ttl_for(ttl) or 300, **opts)
        return self.api.records[rid]

    async def update(self, zone, record, value, ttl, opts):
        self.api.step('update', record.name, value)
        known = self.api.records[record.id]
        known.value = value
        known.ttl = self.ttl_for(ttl) or known.ttl
        known.extra.update(opts)

    async def delete(self, zone, record):
        self.api.step('delete', record.name, record.value)
        self.api.records.pop(record.id, None)


class FakePushProvider(Provider):
    type = 'fakepush'
    name = 'FakePush'
    fields = (Field('token', 'Token', 'password'), Field('zones', '可管理的域', optional=True))
    caps = Caps(zones=False, read=False, by_id=False, delete=False, multi=False)

    def __init__(self, creds, settings=None, *, client=None):
        super().__init__(creds, settings, client=client or object())
        self.api = store(self.creds['token'])
        # Like rfc2136 and callback: configured zones make this instance list them.
        self.managed = [z.strip() for z in self.creds.get('zones', '').split(',') if z.strip()]
        if self.managed:
            self.caps = Caps(zones=True, read=False, by_id=False, delete=False, multi=False)

    async def check(self):
        self.api.step('check')
        if not self.managed:
            raise ProviderError('unsupported', 'no zones configured')
        return {'zones': list(self.managed), 'writable': None}

    async def update(self, zone, record, value, ttl, opts):
        self.api.step('update', record.name, value)
        found = [r for r in self.api.records.values() if r.name == record.name and r.type == record.type]
        if found:
            found[0].value = value
        else:
            self.api.add(record.name, record.type, value)


class FakeNameProvider(FakeProvider):
    """Records located by name, type and value: what it is handed back is all it knows of one."""
    type = 'fakename'
    name = 'FakeName'
    caps = Caps(by_id=False, ttl_min=60, ttl_max=86400)

    async def records(self, zone, name, rtype):
        return [Record('', r.name, r.type, r.value, r.ttl, dict(r.extra)) for r in await super().records(zone, name, rtype)]

    def _find(self, record):
        return next(k for k, r in self.api.records.items() if (r.name, r.type, r.value) == (record.name, record.type, record.value))

    async def create(self, zone, name, rtype, value, ttl, opts):
        await super().create(zone, name, rtype, value, ttl, opts)
        return ''

    async def update(self, zone, record, value, ttl, opts):
        await super().update(zone, Record(self._find(record), record.name, record.type, record.value), value, ttl, opts)

    async def delete(self, zone, record):
        await super().delete(zone, Record(self._find(record), record.name, record.type, record.value))


class FakeZoneProvider(FakeNameProvider):
    """Every write reads the zone, waits on the network, and puts the whole zone back."""
    type = 'fakezone'
    name = 'FakeZone'
    caps = Caps(by_id=False, ttl_min=60, ttl_max=86400, whole_zone=True)

    async def _rewrite(self, change):
        zone = {k: Record(r.id, r.name, r.type, r.value, r.ttl, dict(r.extra)) for k, r in self.api.records.items()}
        change(zone)
        # Long enough for another sync of the zone to get this far meanwhile.
        await asyncio.sleep(0.05)
        self.api.records = zone

    async def create(self, zone, name, rtype, value, ttl, opts):
        self.api.step('create', name, value)
        rid = f'rec{next(_ids)}'
        await self._rewrite(lambda z: z.__setitem__(rid, Record(rid, name, rtype, value, ttl or 300)))
        return ''

    async def update(self, zone, record, value, ttl, opts):
        self.api.step('update', record.name, value)
        key = self._find(record)
        await self._rewrite(lambda z: setattr(z[key], 'value', value))


def install(monkeypatch):
    """Register them for one test only, so other tests see the registry as it is."""
    reset()
    for cls in (FakeProvider, FakePushProvider, FakeNameProvider, FakeZoneProvider):
        monkeypatch.setitem(REGISTRY, cls.type, cls)
