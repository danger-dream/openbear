"""NS1 (IBM NS1 Connect) API v1: https://developer.ibm.com/apis/catalog/ns1--ibm-ns1-connect-api/

A record is addressed by zone, domain and type and holds a list of answers, so values have no
id of their own. POST is a partial update: sending answers (and ttl) leaves filters, metadata and
the other record settings untouched; each kept answer is sent back with its own metadata.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class NS1(Provider):
    type = 'ns1'
    name = 'NS1 (IBM NS1 Connect)'
    tier = 'A'
    fields = (Field('key', 'API 密钥', 'password', hint='在 Account settings → API keys 创建密钥并填写其 secret；权限只勾选 DNS 下的 View zones 和 Manage zones，可限定到指定 zone'),)
    caps = Caps(by_id=False, ttl_min=1, ttl_max=86400)
    docs = 'https://www.ibm.com/docs/en/SSSYGZ/account_management/create_an_api_key.html'
    endpoint = 'https://api.nsone.net/v1'

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'X-NSONE-Key': self.creds['key']}, **kwargs)
        if response.status_code < 300:
            return response
        try:
            message = str(response.json().get('message') or '')
        except (ValueError, AttributeError):
            message = ''
        if response.status_code == 404 and message == 'record not found':
            return None
        if response.status_code == 404 and message == 'zone not found':
            raise ProviderError('zone_not_found', message, status=404)
        raise self.http_error(response, message)

    async def zones(self):
        out, url = [], '/zones'
        while url:
            response = await self._api('GET', url)
            out += [(z.get('zone') or z.get('name')).lower() for z in self.json(response)]
            url = response.links.get('next', {}).get('url')
        return sorted(set(out))

    async def _record(self, zone, name, rtype):
        response = await self._api('GET', f'/zones/{zone}/{name}/{rtype}')
        return self.json(response) if response is not None else None

    async def records(self, zone, name, rtype):
        record = await self._record(zone, name, rtype)
        if not record:
            return []
        return [Record('', name, rtype, a['answer'][0], record.get('ttl')) for a in record.get('answers') or [] if a.get('answer')]

    async def _post(self, zone, name, rtype, answers, ttl):
        body = {'answers': answers}
        if ttl:
            body['ttl'] = ttl
        await self._api('POST', f'/zones/{zone}/{name}/{rtype}', json=body)

    async def create(self, zone, name, rtype, value, ttl, opts):
        ttl = self.ttl_for(ttl)
        record = await self._record(zone, name, rtype)
        if record:
            await self._post(zone, name, rtype, [*record.get('answers', []), {'answer': [value]}], ttl)
        else:
            body = {'zone': zone, 'domain': name, 'type': rtype, 'answers': [{'answer': [value]}]}
            if ttl:
                body['ttl'] = ttl
            await self._api('PUT', f'/zones/{zone}/{name}/{rtype}', json=body)
        return ''

    async def update(self, zone, record, value, ttl, opts):
        current = await self._record(zone, record.name, record.type)
        if not current:
            await self.create(zone, record.name, record.type, value, ttl, opts)
            return
        answers = current.get('answers') or []
        target = next((a for a in answers if a.get('answer') == [record.value]), None)
        if target is None:
            answers.append(target := {})
        target['answer'] = [value]
        await self._post(zone, record.name, record.type, answers, self.ttl_for(ttl))

    async def delete(self, zone, record):
        current = await self._record(zone, record.name, record.type)
        if not current:
            return
        answers = current.get('answers') or []
        rest = [a for a in answers if a.get('answer') != [record.value]]
        if len(rest) == len(answers):
            return
        if rest:
            await self._post(zone, record.name, record.type, rest, None)
        else:
            await self._api('DELETE', f'/zones/{zone}/{record.name}/{record.type}')
