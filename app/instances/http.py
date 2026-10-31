"""Role-enforced owner APIs for servers, the access gateway and instances."""
import json
import sqlite3

from aiohttp import web
from pydantic import ValidationError

from app.instances.contracts import InstanceError


def install(app, host, service):
    from app.web_console.core import _WEB_SESSION_KEY

    async def body(request):
        limit = 1024 * 1024
        if request.content_length and request.content_length > limit:
            raise InstanceError('body_too_large', 413)
        raw = bytearray()
        async for chunk in request.content.iter_chunked(65536):
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

    def wrap(fn):
        async def handler(request):
            try:
                service.require_controller()
                session = request.get(_WEB_SESSION_KEY)
                if session is None:
                    raise InstanceError('authentication_required', 401)
                owner = int(session.chat_id)
                whitelist = getattr(getattr(host.config, 'telegram', None), 'whitelist_ids', [])
                if owner not in {int(v) for v in whitelist}:
                    raise InstanceError('owner_required', 403)
                result = await fn(request, owner)
                return web.json_response(result, headers={'Cache-Control': 'no-store'})
            except InstanceError as exc:
                return web.json_response({'ok': False, 'error': exc.error}, status=exc.status)
            except (ValidationError, ValueError, TypeError, OverflowError):
                # ValidationError.__str__ may contain raw submitted passwords.
                return web.json_response({'ok': False, 'error': 'invalid_parameters'}, status=422)
            except sqlite3.Error:
                return web.json_response({'ok': False, 'error': 'storage_unavailable'}, status=503)
        return handler

    def port(r):
        try:
            return int(r.query.get('port', ''))
        except ValueError:
            raise InstanceError('invalid_port') from None

    async def listing(r, o): return await service.list(o)
    async def create(r, o): return await service.create(o, await body(r))
    async def get(r, o): return await service.get(o, r.match_info['id'])
    async def patch(r, o):
        await service.row(o, r.match_info['id'])
        return await service.save(o, await body(r), r.match_info['id'])
    async def deploy(r, o): return await service.start_operation(o, r.match_info['id'], 'deployment', await body(r))
    async def maintenance(r, o): return await service.start_operation(o, r.match_info['id'], 'maintenance', await body(r))
    async def verify(r, o): return await service.verify(o, r.match_info['id'])
    async def operations(r, o): return await service.operations(o, r.match_info['id'])
    async def events(r, o): return await service.events(o, r.match_info['id'], **dict(r.query))
    async def relay(r, o): return await service.request(o, r.match_info['id'], await body(r))
    async def archive(r, o): return await service.archive(o, r.match_info['id'])
    async def gateway_sync(r, o): return await service.gateway.sync(o, r.match_info['id'])
    async def rotate_keys(r, o): return await service.rotate_keys(o, r.match_info['id'])
    async def migrate_network(r, o): return await service.migrate_network(o, r.match_info['id'])
    async def folder(r, o): return await service.folder(o)
    async def model_options(r, o): return service.model_options()
    async def gateway_get(r, o): return await service.gateway.get()
    async def gateway_enable(r, o): return await service.gateway.enable(o, await body(r))
    async def gateway_disable(r, o): return await service.gateway.disable(o)
    async def gateway_url(r, o): return await service.gateway.set_public_url(o, (await body(r)).get('publicUrl'))
    async def gateway_ip(r, o): return await service.gateway.set_client_ip(await body(r))
    async def gateway_rotate(r, o): return await service.gateway.rotate_all(o)
    async def gateway_update(r, o): return await service.gateway.update(o)
    async def gateway_auto_update(r, o): return await service.gateway.set_auto_update(await body(r))
    async def gateway_port(r, o): return await service.gateway.check_port(o, r.query.get('serverId', ''), port(r))
    async def gateway_suggest(r, o): return await service.gateway.suggest(o, r.query.get('serverId', ''), port(r))
    async def servers(r, o): return await service.servers.list(o)
    async def server_create(r, o): return await service.servers.save(o, await body(r))
    async def server_update(r, o): return await service.servers.save(o, await body(r), r.match_info['id'])
    async def server_delete(r, o): return await service.servers.delete(o, r.match_info['id'])
    async def server_check(r, o): return await service.servers.check(o, r.match_info['id'])
    async def server_port(r, o): return await service.servers.port(o, r.match_info['id'], port(r))
    async def server_test(r, o):
        data = await body(r)
        item = service.servers._validate(data, {})
        secret = await service.servers.secret_for_test(data)
        return {'ok': True, **(await service.servers.test_target({**item, **secret}))['facts']}
    async def ssh_keys(r, o):
        from app.instances.keys import catalog
        return {'ok': True, 'items': await catalog(service.db)}
    for method, path, fn in [
        ('GET', '/api/instance-gateway', gateway_get), ('POST', '/api/instance-gateway/enable', gateway_enable),
        ('POST', '/api/instance-gateway/disable', gateway_disable), ('PUT', '/api/instance-gateway/public-url', gateway_url),
        ('PUT', '/api/instance-gateway/client-ip', gateway_ip), ('GET', '/api/instance-gateway/port', gateway_port),
        ('GET', '/api/instance-gateway/suggest', gateway_suggest), ('POST', '/api/instance-gateway/rotate-keys', gateway_rotate),
        ('POST', '/api/instance-gateway/update', gateway_update), ('PUT', '/api/instance-gateway/auto-update', gateway_auto_update),
        ('GET', '/api/instance-servers', servers), ('POST', '/api/instance-servers', server_create),
        ('POST', '/api/instance-servers/test', server_test), ('GET', '/api/instance-servers/ssh-keys', ssh_keys),
        ('PATCH', '/api/instance-servers/{id}', server_update), ('DELETE', '/api/instance-servers/{id}', server_delete),
        ('POST', '/api/instance-servers/{id}/check', server_check), ('GET', '/api/instance-servers/{id}/port', server_port),
        ('GET', '/api/instance-deployment-folder', folder), ('GET', '/api/instance-model-options', model_options),
    ]:
        app.router.add_route(method, path, wrap(fn))
    for method, path, fn in [
        ('GET', '', listing), ('POST', '', create), ('GET', '/{id}', get), ('PATCH', '/{id}', patch),
        ('POST', '/{id}/deploy', deploy), ('POST', '/{id}/maintenance', maintenance), ('POST', '/{id}/gateway-sync', gateway_sync),
        ('POST', '/{id}/verify', verify), ('GET', '/{id}/operations', operations), ('GET', '/{id}/events', events),
        ('POST', '/{id}/request', relay), ('POST', '/{id}/archive', archive),
        ('POST', '/{id}/rotate-keys', rotate_keys), ('POST', '/{id}/migrate-network', migrate_network),
    ]:
        app.router.add_route(method, '/api/instances'+path, wrap(fn))
