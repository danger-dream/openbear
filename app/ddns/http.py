"""Owner APIs for the 动态域名 page. Every error answers {ok: false, error, message}; no answer
carries a credential value."""
import json
import sqlite3

from aiohttp import web
from pydantic import ValidationError

from app.ddns.model import MESSAGES, message_of
from app.instances.contracts import InstanceError

HEADERS = {'Cache-Control': 'no-store'}


def install(app, host, service):
    from app.web_console.core import _WEB_SESSION_KEY

    async def body(request, limit=256 * 1024):
        if request.content_length and request.content_length > limit:
            raise InstanceError('body_too_large', 413)
        raw = bytearray()
        while True:
            chunk = await request.content.read(min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > limit:
                raise InstanceError('body_too_large', 413)
        try:
            value = json.loads(raw or '{}')
        except (ValueError, UnicodeDecodeError):
            raise InstanceError('invalid_json') from None
        if not isinstance(value, dict):
            raise InstanceError('json_object_required')
        return value

    def failure(error, status, message=''):
        return web.json_response({'ok': False, 'error': error, 'message': message or MESSAGES.get(error, '') or '操作失败'}, status=status, headers=HEADERS)

    def owner_of(request):
        service.instances.require_controller()
        session = request.get(_WEB_SESSION_KEY)
        if session is None:
            raise InstanceError('authentication_required', 401)
        owner = int(session.chat_id)
        if owner not in {int(v) for v in getattr(getattr(host.config, 'telegram', None), 'whitelist_ids', [])}:
            raise InstanceError('owner_required', 403)
        return owner

    def wrap(fn):
        async def handler(request):
            try:
                return web.json_response(await fn(request, owner_of(request)), headers=HEADERS)
            except InstanceError as exc:
                return failure(exc.error, exc.status, message_of(exc))
            except (ValidationError, ValueError, TypeError, OverflowError):
                return failure('invalid_parameters', 422)
            except sqlite3.Error:
                return failure('storage_unavailable', 503)
        return handler

    def flag(r, name):
        return r.query.get(name, '') in ('1', 'true')

    def limit(r):
        try:
            return int(r.query.get('limit') or 50)
        except ValueError:
            raise InstanceError('invalid_parameters') from None

    async def overview(r, o): return await service.ddns.overview(o)
    async def test_account(r, o): return await service.ddns.test_account(o, await body(r))
    async def add_account(r, o): return await service.ddns.create_account(o, await body(r))
    async def edit_account(r, o): return await service.ddns.update_account(o, r.match_info['id'], await body(r))
    async def delete_account(r, o): return await service.ddns.delete_account(o, r.match_info['id'], force=flag(r, 'force'))
    async def check_account(r, o): return await service.ddns.check_account(o, r.match_info['id'], await body(r))
    async def preview(r, o): return await service.ddns.preview(o, await body(r))
    async def add_records(r, o): return await service.ddns.create_records(o, await body(r))
    async def edit_record(r, o): return await service.ddns.update_record(o, r.match_info['id'], await body(r))
    async def delete_record(r, o): return await service.ddns.delete_record(o, r.match_info['id'], delete_remote=flag(r, 'deleteRemote'))
    async def sync(r, o): return await service.ddns.sync_now(o, r.match_info['id'])
    async def events(r, o): return await service.ddns.events(o, r.match_info['id'], limit(r))

    async def diagnose(r, o):
        data = await body(r)
        return await service.ddns.diagnose(o, data.get('serverId'), data.get('family'))

    async def import_preview(r, o): return await service.ddns.import_preview(o, await body(r, 1024 * 1024))
    async def import_apply(r, o): return await service.ddns.import_apply(o, await body(r, 1024 * 1024))

    for method, path, fn in (
        ('GET', '', overview),
        ('POST', '/accounts/test', test_account),
        ('POST', '/accounts', add_account),
        ('PATCH', '/accounts/{id}', edit_account),
        ('DELETE', '/accounts/{id}', delete_account),
        ('POST', '/accounts/{id}/check', check_account),
        ('POST', '/preview', preview),
        ('POST', '/records', add_records),
        ('PATCH', '/records/{id}', edit_record),
        ('DELETE', '/records/{id}', delete_record),
        ('POST', '/records/{id}/sync', sync),
        ('GET', '/records/{id}/events', events),
        ('POST', '/diagnose', diagnose),
        ('POST', '/import/preview', import_preview),
        ('POST', '/import', import_apply),
    ):
        app.router.add_route(method, '/api/ddns' + path, wrap(fn))
