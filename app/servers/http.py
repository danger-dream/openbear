"""Owner APIs for the Servers page and the public node endpoints. Node routes
bypass cookie auth by name; each authenticates on its own (one-time code,
signed challenge) or serves only non-secret files."""
import json
import re
import sqlite3
import time
from urllib.parse import quote

from aiohttp import web
from pydantic import ValidationError

from app.instances.contracts import InstanceError
from app.servers import install as installer
from app.servers.hub import handshake_source
from app.servers.service import FILE_CHUNK

PUBLIC_ROUTES = frozenset({'server_node_enroll', 'server_node_connect', 'server_node_install', 'server_node_install_windows', 'server_node_download',
                           'server_node_hello'})
UPLOAD_LIMIT = 4 * 1024 ** 3
# A file that says it is empty (/proc and the like) is read until it ends, up to this much.
UNSIZED_DOWNLOAD_LIMIT = 64 * 1024 ** 2


def install(app, host, service):
    from app.web_console.core import _WEB_SESSION_KEY
    failures = {}  # source address -> monotonic times of its failed registrations

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

    def failure(exc):
        extra = {'errors': exc.errors} if getattr(exc, 'errors', None) else {}
        if getattr(exc, 'field', None):
            extra['field'] = exc.field
        return web.json_response({'ok': False, 'error': exc.error, **extra}, status=exc.status, headers={'Cache-Control': 'no-store'})

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
                return web.json_response(await fn(request, owner_of(request)), headers={'Cache-Control': 'no-store'})
            except InstanceError as exc:
                return failure(exc)
            except (ValidationError, ValueError, TypeError, OverflowError):
                return web.json_response({'ok': False, 'error': 'invalid_parameters'}, status=422)
            except sqlite3.Error:
                return web.json_response({'ok': False, 'error': 'storage_unavailable'}, status=503)
        return handler

    def command_id(r):
        try:
            return int(r.match_info['cid'])
        except ValueError:
            raise InstanceError('command_not_found', 404) from None

    async def listing(r, o): return await service.list(o)
    async def commands(r, o): return await service.commands(o)
    async def add_command(r, o): return await service.save_command(o, await body(r))
    async def order_commands(r, o): return await service.order_commands(o, await body(r))
    async def order_servers(r, o): return await service.order(o, await body(r))
    async def edit_command(r, o): return await service.save_command(o, await body(r), command_id(r))
    async def delete_command(r, o): return await service.delete_command(o, command_id(r))
    async def create(r, o): return await service.create(o, await body(r))
    async def live(r, o): return await service.live(o)
    async def settings(r, o): return {'ok': True, 'settings': await service.settings_view(o)}
    async def save_settings(r, o): return await service.save_settings(await body(r), o)
    async def get(r, o): return {'ok': True, 'item': await service.get(o, r.match_info['id'])}
    async def update(r, o): return await service.update(o, r.match_info['id'], await body(r))
    async def delete(r, o): return await service.delete(o, r.match_info['id'])
    def browser_flag(data):
        if type(data.get('browser', False)) is not bool:
            raise InstanceError('invalid_browser_settings')
        return data.get('browser', False)

    async def enrollment(r, o): return await service.enrollment(o, r.match_info['id'], browser=browser_flag(await body(r)))
    async def node_install(r, o):
        data = await body(r)
        return await service.install(o, r.match_info['id'], browser=browser_flag(data), force=data.get('force', False))
    async def revoke(r, o): return await service.revoke(o, r.match_info['id'], uninstall=r.query.get('uninstall') == '1')
    async def settle_join(r, o): return await service.settle_join(o, r.match_info['id'], await body(r))
    async def metrics(r, o): return await service.series(o, r.match_info['id'], r.query.get('range', '1h'))
    async def reminders(r, o): return {'ok': True, 'items': await service.reminders.recent(o)}
    async def test_reminder(r, o): return await service.reminders.test(o)
    async def batch(r, o): return await service.exec_batch(o, await body(r))
    async def job(r, o): return await service.exec_job(o, r.match_info['job'])
    async def update_all(r, o): return await service.update_nodes(o)
    async def push_url(r, o): return await service.push_node_url(o)
    async def update_one(r, o): return await service.update_node(o, r.match_info['id'], downgrade=(await body(r)).get('downgrade') is True)
    async def renew(r, o): return await service.renew(o, r.match_info['id'])
    async def files(r, o): return await service.list_files(o, r.match_info['id'], r.query.get('path', '/'))
    async def file_op(r, o): return await service.file_op(o, r.match_info['id'], await body(r))
    async def file_job(r, o): return await service.file_op_job(o, r.match_info['id'], r.match_info['job'])
    async def test_access(r, o): return await service.onboarding.test_access((await body(r)).get('url'))
    async def codes(r, o): return await service.onboarding.codes(o)
    async def add_code(r, o): return await service.onboarding.create_code(o, await body(r))
    async def revoke_code(r, o): return await service.onboarding.revoke_code(o, r.match_info['code'])
    async def batch_install(r, o): return await service.onboarding.batch(o, await body(r))
    async def batch_status(r, o): return await service.onboarding.batch_job(o, r.match_info['job'])
    async def node_release(r, o): return await service.node_release()
    async def check_node_release(r, o): return await service.node_release(check=True)
    async def browser(r, o): return await service.browser_status(o, r.match_info['id'])
    async def set_browser(r, o): return await service.set_browser(o, r.match_info['id'], await body(r))
    async def restart_browser(r, o): return await service.restart_browser(o, r.match_info['id'])
    async def jobs(r, o): return await service.jobs_list(o, r.match_info['id'])

    async def job_status(r, o):
        offset, limit = r.query.get('offset'), r.query.get('limit')
        return await service.job_status(o, r.match_info['id'], r.match_info['job'], offset=int(offset) if offset else None,
                                        limit=min(int(limit), 256 * 1024) if limit else None)
    async def job_stop(r, o): return await service.job_stop(o, r.match_info['id'], r.match_info['job'])
    async def mesh(r, o): return await service.mesh.view(o)
    async def mesh_draft(r, o): return await service.mesh.save_draft(o, await body(r))
    async def mesh_add(r, o): return await service.mesh.add(o, await body(r))
    async def mesh_remove(r, o): return await service.mesh.remove(o, r.match_info['sid'])
    async def mesh_reinstall(r, o): return await service.mesh.reinstall(o, r.match_info['sid'])
    async def mesh_apply(r, o): return await service.mesh.apply(o)
    async def mesh_discard(r, o): return await service.mesh.discard(o)
    async def mesh_rollback(r, o): return await service.mesh.rollback(o)
    async def mesh_member(r, o): return await service.mesh.member(o, r.match_info['sid'])
    async def mesh_trend(r, o): return await service.mesh.trend(o, r.match_info['sid'], r.query.get('range', '1h'))
    async def mesh_line_trend(r, o): return await service.mesh.line_trend(o, r.match_info['line'], r.query.get('range', '24h'))
    async def mesh_layout(r, o): return await service.mesh.save_layout(o, await body(r))
    async def mesh_path(r, o): return await service.mesh.path(o, r.query.get('src', ''), r.query.get('dst', ''))
    async def mesh_routes(r, o): return await service.mesh.routes(o, r.query.get('src', ''))
    async def net_addresses(r, o): return await service.net.addresses(o, r.match_info['id'])
    async def net_probe(r, o): return await service.net.start(o, r.match_info['id'], await body(r))
    async def net_probe_status(r, o): return await service.net.status(o, r.match_info['id'], r.match_info['pid'])
    async def net_probe_stop(r, o): return await service.net.stop(o, r.match_info['id'], r.match_info['pid'])
    async def bench_catalog(r, o): return service.bench.catalog()
    async def bench_runs(r, o): return await service.bench.runs(o)
    async def bench_create(r, o): return await service.bench.create(o, await body(r))
    async def bench_run(r, o): return await service.bench.run(o, r.match_info['run'])
    async def bench_stop(r, o): return await service.bench.stop(o, r.match_info['run'])
    async def bench_delete(r, o): return await service.bench.delete(o, r.match_info['run'])
    # Fixed paths are registered before /{id} so they are never taken as an id.
    for method, path, fn in [
        ('GET', '', listing), ('POST', '', create), ('PUT', '/order', order_servers), ('GET', '/live', live), ('GET', '/settings', settings),
        ('GET', '/commands', commands), ('POST', '/commands', add_command), ('PUT', '/commands/order', order_commands),
        ('PATCH', '/commands/{cid}', edit_command), ('DELETE', '/commands/{cid}', delete_command),
        ('PUT', '/settings', save_settings), ('GET', '/reminders', reminders), ('POST', '/reminders/test', test_reminder),
        ('POST', '/exec', batch), ('GET', '/exec/{job}', job), ('POST', '/node-update', update_all), ('POST', '/node-url', push_url),
        ('POST', '/access-points/test', test_access), ('GET', '/registration-codes', codes), ('POST', '/registration-codes', add_code),
        ('DELETE', '/registration-codes/{code}', revoke_code), ('POST', '/batch-install', batch_install), ('GET', '/batch-install/{job}', batch_status),
        ('GET', '/node-release', node_release), ('POST', '/node-release/check', check_node_release),
        ('GET', '/mesh', mesh), ('PUT', '/mesh/draft', mesh_draft), ('POST', '/mesh/members', mesh_add), ('DELETE', '/mesh/members/{sid}', mesh_remove),
        ('POST', '/mesh/apply', mesh_apply), ('POST', '/mesh/discard', mesh_discard), ('POST', '/mesh/rollback', mesh_rollback),
        ('POST', '/mesh/members/{sid}/reinstall', mesh_reinstall), ('GET', '/mesh/members/{sid}', mesh_member), ('GET', '/mesh/members/{sid}/trend', mesh_trend),
        ('GET', '/mesh/lines/{line}/trend', mesh_line_trend), ('PUT', '/mesh/layout', mesh_layout),
        ('GET', '/mesh/path', mesh_path), ('GET', '/mesh/routes', mesh_routes),
        ('GET', '/bench/catalog', bench_catalog), ('GET', '/bench/runs', bench_runs), ('POST', '/bench/runs', bench_create),
        ('GET', '/bench/runs/{run}', bench_run), ('POST', '/bench/runs/{run}/stop', bench_stop), ('DELETE', '/bench/runs/{run}', bench_delete),
        ('GET', '/{id}', get), ('PATCH', '/{id}', update), ('DELETE', '/{id}', delete), ('POST', '/{id}/enrollment', enrollment),
        ('POST', '/{id}/install', node_install), ('DELETE', '/{id}/node', revoke), ('POST', '/{id}/join', settle_join), ('GET', '/{id}/metrics', metrics),
        ('POST', '/{id}/node/update', update_one), ('POST', '/{id}/renew', renew), ('GET', '/{id}/files', files),
        ('POST', '/{id}/files/op', file_op), ('GET', '/{id}/files/op/{job}', file_job),
        ('GET', '/{id}/browser', browser), ('PUT', '/{id}/browser', set_browser), ('POST', '/{id}/browser/restart', restart_browser),
        ('GET', '/{id}/jobs', jobs), ('GET', '/{id}/jobs/{job}', job_status), ('POST', '/{id}/jobs/{job}/stop', job_stop),
        ('GET', '/{id}/net/addresses', net_addresses), ('POST', '/{id}/net/probes', net_probe),
        ('GET', '/{id}/net/probes/{pid}', net_probe_status), ('DELETE', '/{id}/net/probes/{pid}', net_probe_stop),
    ]:
        app.router.add_route(method, '/api/servers' + path, wrap(fn))

    async def terminal(request):
        # Cookie session and Origin were checked by the console middleware.
        try:
            owner = owner_of(request)
            await service.registry.get(owner, request.match_info['id'])
        except InstanceError as exc:
            return failure(exc)
        return await service.terminal(request, owner, request.match_info['id'])
    app.router.add_get('/api/servers/{id}/terminal', terminal)

    async def download(request):
        path = request.query.get('path', '')
        try:
            owner = owner_of(request)
            server_id = request.match_info['id']
            await service.registry.get(owner, server_id)
            info = await service.stat_file(owner, server_id, path)
            if info.get('dir'):
                raise InstanceError('is_directory', 409)
            first = await service.read_file(owner, server_id, path)
            # The size of the file the node opened (a link's target, not the link). It does not get to
            # send more than that, and one that does not say gets nothing; 0 is read to its end.
            size = first.get('size')
            if type(size) is not int or size < 0:
                raise InstanceError('file_size_unknown', 502)
        except InstanceError as exc:
            return failure(exc)
        response = web.StreamResponse(headers={'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                               'Content-Disposition': f"attachment; filename*=UTF-8''{quote(info.get('name') or 'download')}"})
        # Files of /sys and /proc claim a size (4096) they do not have: sent without a length,
        # they end where the node read their end.
        pseudo = re.match(r'/(sys|proc)(/|$)', path) is not None
        if size and not pseudo:
            response.content_length = size
        limit = size or UNSIZED_DOWNLOAD_LIMIT
        await response.prepare(request)
        chunk, offset, complete = first, 0, False
        while True:
            data = chunk['data'][:limit - offset]
            await response.write(data)
            offset += len(data)
            ended = chunk.get('eof') or not chunk['data']
            if offset >= limit or ended:
                read_to_end = bool(ended) and len(data) == len(chunk['data'])
                complete = offset >= size or pseudo and read_to_end if size else read_to_end
                break
            try:
                chunk = await service.read_file(owner, server_id, path, offset)
            except InstanceError:
                break
        # After the headers are sent a failure can only cut the download short: the connection is
        # closed so the browser sees it short of its length or unfinished, not as a whole file.
        if not complete:
            if request.transport is not None:
                request.transport.close()
            return response
        await response.write_eof()
        return response

    async def upload(request):
        path, mode = request.query.get('path', ''), request.query.get('mode', '')
        try:
            owner = owner_of(request)
            server_id = request.match_info['id']
            await service.registry.get(owner, server_id)
            # Refused before the first chunk reaches the node, not after 4 GB of it.
            if request.content_length and request.content_length > UPLOAD_LIMIT:
                raise InstanceError('body_too_large', 413)
            if request.query.get('overwrite') != '1':
                try:
                    await service.stat_file(owner, server_id, path)
                    raise InstanceError('file_exists', 409)
                except InstanceError as exc:
                    if exc.error != 'not_found':
                        raise

            async def chunks():
                total, buffer = 0, bytearray()
                while True:
                    piece = await request.content.read(FILE_CHUNK - len(buffer))
                    total += len(piece)
                    if total > UPLOAD_LIMIT:
                        raise InstanceError('body_too_large', 413)
                    buffer += piece
                    if len(buffer) >= FILE_CHUNK or not piece:
                        yield bytes(buffer)
                        buffer.clear()
                    if not piece:
                        return
            result = await service.write_file(owner, server_id, path, chunks(), mode=mode, mkdirs=request.query.get('mkdirs') == '1', label='上传文件')
        except InstanceError as exc:
            return failure(exc)
        return web.json_response(result, headers={'Cache-Control': 'no-store'})
    app.router.add_get('/api/servers/{id}/files/download', download)
    app.router.add_post('/api/servers/{id}/files/upload', upload)

    async def enroll(request):
        # Failed registrations count per source: one guessing codes does not lock everyone else out.
        now, source = time.monotonic(), handshake_source(request)
        if len(failures) > 4096:
            for stale in [s for s, times in failures.items() if now - times[-1] >= 600]:
                del failures[stale]
        mine = [t for t in failures.get(source, ()) if now - t < 600]
        if len(mine) >= 30:
            return web.json_response({'ok': False, 'error': 'rate_limited'}, status=429)
        try:
            return web.json_response(await service.enroll(await body(request, 64 * 1024)), headers={'Cache-Control': 'no-store'})
        except InstanceError as exc:
            failures[source] = [*mine, now]
            return failure(exc)
        except sqlite3.Error:
            return web.json_response({'ok': False, 'error': 'storage_unavailable'}, status=503)

    async def connect(request):
        return await service.hub.handle(request)

    async def script(request):
        return web.Response(text=installer.script(), content_type='text/x-shellscript', headers={'Cache-Control': 'no-store'})

    async def windows_script(request):
        return web.Response(text=installer.windows_script(), content_type='text/plain', charset='utf-8', headers={'Cache-Control': 'no-store'})

    async def download(request):
        # Only names the manifest lists; a release binary is fetched from GitHub once, then cached.
        path = await installer.fetch_binary(request.match_info['name'])
        if path is None:
            return web.json_response({'ok': False, 'error': 'not_found'}, status=404)
        return web.FileResponse(path, headers={'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store'})

    async def hello(request):
        # Lets an access point be checked: which controller answers here. Public, no secret.
        from app import __version__
        return web.json_response({'ok': True, 'service': 'openbear', 'controller': service.onboarding.fingerprint(), 'version': __version__},
                                 headers={'Cache-Control': 'no-store'})

    app.router.add_get('/api/server-nodes/v1/hello', hello, name='server_node_hello')
    app.router.add_post('/api/server-nodes/v1/enroll', enroll, name='server_node_enroll')
    app.router.add_get('/api/server-nodes/v1/connect', connect, name='server_node_connect')
    app.router.add_get('/api/server-nodes/v1/install.sh', script, name='server_node_install')
    app.router.add_get('/api/server-nodes/v1/install.ps1', windows_script, name='server_node_install_windows')
    app.router.add_get('/api/server-nodes/v1/download/{name}', download, name='server_node_download')
