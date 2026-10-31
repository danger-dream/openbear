"""Controller instance registry, operation authority and restart-safe reconciliation."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shlex
import time
import uuid
from pathlib import Path

import aiohttp

from app.instances import network
from app.instances.contracts import Form, InstanceError, check_workspace, origin
from app.instances.schema import migrate
from app.instances.security import Scrubber, Vault, secret_values
from app.lifecycle import wait_for_startup
from app.notice_names import quoted
from app.webhooks.repository import many, one

FOLDER_NAME = '实例部署'
FOLDER_PROMPT = ('这里是 OpenBear 实例的部署与维护会话。每个会话已绑定一个实例和本次操作，'
                 '通过 InstanceControl 读取目标、执行命令并报告结果。')
FIRST_PORT = 18970
# Key rotation retries while an instance's server cannot be reached.
ROTATION_RETRY_MS = 30000
ROTATION_RETRY_MAX_MS = 600000


def now():
    return int(time.time() * 1000)


OPERATION_KINDS = {'deployment': '部署', 'maintenance': '维护'}
# How an operation ended, as its notice says it.
OPERATION_ENDS = {'completed': '已完成。', 'needs_verification': '结束了，但没能确认结果：请到实例页点「立即检查」，或在会话里看过程。',
                  'failed': '失败了，原因见会话。', 'interrupted': '中断了，可以在会话里继续。'}


def uid():
    return str(uuid.uuid4())


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def merge_profile(previous, patch):
    result = dict(previous)
    for key, value in patch.items():
        result[key] = merge_profile(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


class InstanceService:
    def __init__(self, db, host):
        self.db, self.host = db, host
        self.vault = Vault(db)
        self.http = None
        self.tasks = {}
        self.locks = {}
        self.worker = None
        self.monitor_task = self.recovery_task = None
        self.closing = False
        from app.instances.gateway import Gateway
        from app.instances.servers import Servers
        self.gateway = Gateway(self)
        self.servers = Servers(self)

    @property
    def managed(self):
        return bool(getattr(getattr(getattr(self.host, 'config', None), 'managed', None), 'enabled', False))

    def require_controller(self):
        if self.managed:
            raise InstanceError('controller_only', 403)

    def lock(self, identity):
        return self.locks.setdefault(identity, asyncio.Lock())

    async def start(self):
        self.require_controller()
        await migrate(self.db)
        await self.servers.migrate()
        if self.http is not None:
            return
        self.closing = False
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        # No network I/O on startup. Bounded background reconciliation never
        # recreates a model run or replays deployment commands.
        from app.instances.monitor import monitor, recover
        recovery_rows = await many(self.db.conn, "SELECT * FROM instance_operations WHERE status IN ('pending','running','needs_verification','interrupted')")
        self.recovery_task = asyncio.create_task(recover(self, recovery_rows), name='instance-recovery')
        self.monitor_task = asyncio.create_task(monitor(self), name='instance-health')
        self.worker = asyncio.create_task(self._worker(), name='instance-notifications')

    async def close(self):
        self.closing = True
        background = [t for t in (self.recovery_task, self.monitor_task, self.gateway.update_task) if t]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self.recovery_task = self.monitor_task = self.gateway.update_task = None
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
            self.worker = None
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.http:
            await self.http.close()
            self.http = None

    async def row(self, owner, identity, *, active=True):
        self.require_controller()
        row = await one(self.db.conn, 'SELECT * FROM managed_instances WHERE id=? AND owner=?', (identity, owner))
        if row is None:
            raise InstanceError('instance_not_found', 404)
        if active and row['status'] == 'archived':
            raise InstanceError('instance_archived', 409)
        return row

    def public(self, row):
        item = json.loads(row['public_json'])
        item.pop('lastHealthAtMs', None)
        item.pop('deploymentAccepted', None)
        item.pop('noticeWindow', None)
        item['authMode'] = row['auth_mode']
        item['status'], item['activeOperationId'] = row['status'], row['active_operation'] or None
        return item

    async def list(self, owner):
        self.require_controller()
        rows = await many(self.db.conn, 'SELECT * FROM managed_instances WHERE owner=? ORDER BY rowid DESC', (owner,))
        return {'ok': True, 'items': [{**self.public(r), 'networkMigration': await self._migration_or_none(owner, r)} for r in rows]}

    async def get(self, owner, identity):
        row = await self.row(owner, identity, active=False)
        return {'ok': True, 'item': {**self.public(row), 'networkMigration': await self._migration_or_none(owner, row)}}

    async def _models_config(self, models):
        """Copy the chosen enabled controller providers (with keys) into the instance;
        without a choice only the primary model's provider."""
        if models['mode'] != 'copy':
            return {}
        raw = self.host.config.model_dump(by_alias=True)['models']
        primary = models.get('primary') or raw.get('primary', '')
        wanted = models.get('providers')
        if wanted is None:
            wanted = [primary.split('/', 1)[0]]
        providers = {name: value for name, value in raw['providers'].items()
                     if value.get('enabled', True) and name in wanted}
        if primary.split('/', 1)[0] not in providers:
            raise InstanceError('instance_primary_model_not_copied')
        return {'models': {'providers': providers, 'primary': primary}}

    def model_options(self):
        """Enabled controller providers and models for the create form (no keys)."""
        models = self.host.config.models
        providers = [{'name': name, 'models': [m.id for m in p.models]} for name, p in models.providers.items() if p.enabled]
        return {'ok': True, 'primary': models.primary, 'providers': providers}

    async def _allocate_port(self, owner, server_id, exclude):
        taken = set(exclude)
        for row in await many(self.db.conn, "SELECT public_json FROM managed_instances WHERE status!='archived'"):
            item = json.loads(row['public_json'])
            if item.get('serverId') == server_id and item.get('port'):
                taken.add(item['port'])
        value, private = await self.gateway.config()
        if value['enabled'] and value['serverId'] == server_id:
            taken.update(p for p in (value['port'], private.get('controlPort')) if p)
        for port in range(FIRST_PORT, FIRST_PORT + 200):
            if port in taken:
                continue
            if (await self.servers.port(owner, server_id, port)).get('free') is not False:
                return port
        raise InstanceError('no_free_port', 409)

    async def _placement(self, owner, server_id, port):
        """(address the gateway uses, network) of a new instance. On the local machine, or a server
        whose node carries the controller's HTTP, the port listens on loopback only, plus the mesh
        address when the gateway runs on another server: then both must be mesh members. Other
        servers keep server address + port, network None."""
        if server_id != 'local' and not await self.servers.http_ready(owner, server_id):
            return await self._address(owner, server_id, port), None
        gateway, _ = await self.gateway.config()
        gateway_server = gateway['serverId'] or 'local'
        if gateway_server == server_id:
            return origin(f'http://127.0.0.1:{port}'), {'bind': ['127.0.0.1']}
        mine, theirs = await self.servers.mesh_address(owner, server_id), await self.servers.mesh_address(owner, gateway_server)
        if not mine or not theirs:
            raise InstanceError('instance_mesh_required', 409)
        return origin(f'http://{mine}:{port}'), {'bind': ['127.0.0.1', mine]}

    def _mesh_changed(self, owner=None):
        """Instances listening on a mesh address are let through by the mesh's automatic rules: a
        change to one (placed, moved, another port, archived) or to the gateway (owner None: every
        owner) brings the mesh configs up to date."""
        mesh = getattr(getattr(self.host, 'servers', None), 'mesh', None)
        if mesh is not None:
            mesh.instances_changed(owner)

    async def check_placement(self, owner, item):
        """A mesh-bound instance deploys only while both servers are still where it was placed."""
        from app.instances.remote import mesh_bind
        address = mesh_bind(item)
        if not address:
            return
        gateway, _ = await self.gateway.config()
        if (await self.servers.mesh_address(owner, item['serverId']) != address
                or not await self.servers.mesh_address(owner, gateway['serverId'] or 'local')):
            raise InstanceError('instance_mesh_required', 409)

    def _controller_directories(self):
        from app.instances.remote import ROOT
        directories = {ROOT, ROOT / 'data', Path(self.host.config.storage.db_path).resolve().parent}
        if getattr(self.host, 'workspace_dir', ''):
            directories.add(Path(self.host.workspace_dir).resolve())
        return [str(d) for d in directories]

    def _check_workspace(self, form):
        local = form['serverId'] == 'local'
        protected = self._controller_directories() if local else ()
        check_workspace(form['workspacePath'], protected)
        if local:
            check_workspace(os.path.realpath(form['workspacePath']), protected)

    async def _address(self, owner, server_id, port):
        """Server address + port, where both the controller and the gateway reach an
        instance that is not placed on loopback/mesh."""
        if server_id == 'local':
            gateway, _ = await self.gateway.config()
            if gateway['serverId'] in {'', 'local'}:
                return origin(f'http://127.0.0.1:{port}')
            host = await self.servers.public_ip(owner, 'local')
            if not host:
                raise InstanceError('instance_address_required', 409)
        else:
            server = await self.servers.get(owner, server_id)
            host = server['host'] or (server.get('node') or {}).get('address', '')
            if not host:
                raise InstanceError('instance_address_required', 409)
        return origin(f'http://[{host}]:{port}' if ':' in host else f'http://{host}:{port}')

    async def save(self, owner, data, identity=None, *, adopt=None):
        """adopt: {'id', 'managementKey', 'businessKey'} of an already running
        instance (re-registering it); otherwise everything is generated."""
        self.require_controller()
        creating = identity is None
        identity = identity or (adopt or {}).get('id') or uid()
        if creating:
            value, _ = await self.gateway.config()
            if not value['enabled']:
                raise InstanceError('gateway_not_enabled', 409)
        async with self.lock(identity):
            old = None if creating else await self.row(owner, identity)
            if old and old['active_operation']:
                op = await one(self.db.conn, 'SELECT status FROM instance_operations WHERE id=?', (old['active_operation'],))
                if op and op['status'] in {'pending', 'running'}:
                    raise InstanceError('operation_running', 409)
            prior = self.vault.open(old['protected']) if old else {}
            form_data = dict(prior.get('form', {}))
            for key, value in data.items():
                form_data[key] = merge_profile(form_data.get(key, {}), value) if key in {'account', 'notifications', 'models'} and isinstance(value, dict) else value
            chosen = bool(form_data.get('workspacePath'))
            form = Form.model_validate(form_data).model_dump()
            if creating and not form['account']['password']:
                raise InstanceError('initial_gateway_password_required')
            server = await self.servers.get(owner, form['serverId'])
            if old and form['serverId'] != prior['form']['serverId']:
                raise InstanceError('instance_server_immutable', 409)
            # Only a new or moved data directory meets the current rules.
            if creating or form['workspacePath'] != prior['form'].get('workspacePath'):
                await self._own_workspace(form, identity, chosen)
                self._check_workspace(form)
            if creating and not form['port']:
                form['port'] = await self._allocate_port(owner, form['serverId'], ())
            elif creating:
                check = await self.servers.port(owner, form['serverId'], form['port'])
                if check.get('free') is False:
                    raise InstanceError('instance_port_in_use', 409)
            previous = json.loads(old['public_json']) if old else {}
            # A placed instance keeps the port it was deployed with: the record and, on the mesh, the rule
            # opening it to the gateway would follow a new one, the running instance would not.
            if previous.get('network') and form['port'] != prior['form'].get('port'):
                from app.instances.remote import mesh_bind
                raise InstanceError('instance_port_fixed_on_mesh' if mesh_bind(previous) else 'instance_port_fixed', 409)
            if form['managementUrl']:
                address, placed = form['managementUrl'], None
            elif creating:
                address, placed = await self._placement(owner, form['serverId'], form['port'])
            elif previous.get('network'):
                # Where it was placed, until migrate_network moves it.
                address, placed = previous['managementUrl'], previous['network']
            else:
                address, placed = await self._address(owner, form['serverId'], form['port']), None
            protected = {**prior, 'form': {**form, 'account': {'username': form['account']['username']}}}
            if creating:
                protected.update(managementKey=(adopt or {}).get('managementKey') or secrets.token_urlsafe(48),
                                 initialConfig=await self._models_config(form['models']))
                if adopt and adopt.get('businessKey'):
                    protected['businessKey'] = adopt['businessKey']
            item = previous
            previous_account = item.get('account', {}).copy()
            item.update({k: form[k] for k in ('name', 'serverId', 'port', 'workspacePath', 'memoryLimitMb', 'notifications')})
            # The record keeps the image delivered; an empty form image only asks for the controller's version.
            if form['image'] or not item.get('image'):
                item['image'] = form['image']
            if placed:
                item['network'] = placed
            else:
                item.pop('network', None)
            item.update(id=identity, mode='managed', managementUrl=address, serverName=server['name'],
                        account={'username': form['account']['username']}, healthStatus=item.get('healthStatus', 'unknown'),
                        lastSeenAtMs=item.get('lastSeenAtMs'), lastError=item.get('lastError', ''), createdAtMs=item.get('createdAtMs', now()), updatedAtMs=now(),
                        models={'mode': form['models']['mode'], 'primary': (protected.get('initialConfig') or {}).get('models', {}).get('primary', '')} if creating else item.get('models', {}))
            account = {}
            explicit = data.get('account', {})
            if creating or 'username' in explicit and explicit['username'] != previous_account.get('username'):
                account['username'] = form['account']['username']
            if form['account'].get('password'):
                account['password'] = form['account']['password']
            if previous_account:
                item['account'] = previous_account  # Current gateway authority, not desired name.
            if creating or account or address != (old and json.loads(old['public_json']).get('managementUrl')):
                from app.instances.gateway import prepare
                prepare(item, protected, enabled=not item.get('archivePending', False), account=account)
            state = old['status'] if old else 'draft'
            async with self.db.write_transaction(label='instance-save') as conn:
                if old:
                    await conn.execute('UPDATE managed_instances SET public_json=?,protected=?,status=?,revision=revision+1 WHERE id=?',
                        (dumps(item), self.vault.seal(protected), state, identity))
                else:
                    await conn.execute('INSERT INTO managed_instances(id,owner,public_json,protected,report_hash,authority,auth_mode) VALUES(?,?,?,?,?,?,?)',
                        (identity, owner, dumps(item), self.vault.seal(protected), hashlib.sha256(identity.encode()).hexdigest(), address, 'gateway'))
                saved = await one(conn, 'SELECT * FROM managed_instances WHERE id=?', (identity,))
        self._mesh_changed(owner)
        # Accounts reach the gateway now, so the user's login works as soon as
        # the instance is up; a failure stays pending and is retried.
        await self.gateway.sync(owner, identity)
        saved = await one(self.db.conn, 'SELECT * FROM managed_instances WHERE id=?', (identity,))
        return {'ok': True, 'item': self.public(saved)}

    async def create(self, owner, data):
        """Create and immediately start the deployment session."""
        deploy = {k: data.pop(k) for k in ('instructions', 'model') if k in data}
        result = await self.save(owner, data)
        identity = result['item']['id']
        try:
            started = await self.start_operation(owner, identity, 'deployment', deploy)
        except InstanceError as exc:
            return {**result, 'deployment': {'ok': False, 'error': exc.error}}
        return {**await self.get(owner, identity), 'deployment': started, 'conversationUuid': started['conversationUuid']}

    async def operations(self, owner, identity):
        await self.row(owner, identity, active=False)
        return {'ok': True, 'items': [self.operation(r) for r in await many(self.db.conn, 'SELECT * FROM instance_operations WHERE instance_id=? ORDER BY created_ms DESC', (identity,))]}

    @staticmethod
    def operation(row):
        return {'id': row['id'], 'instanceId': row['instance_id'], 'kind': row['kind'], 'status': row['status'],
                'conversationUuid': row['conversation_uuid'], 'revision': row['revision'], 'createdAtMs': row['created_ms'],
                'updatedAtMs': row['updated_ms'], 'error': row['error'], 'report': json.loads(row['report_json'] or 'null'),
                'verification': json.loads(row['verification_json'] or 'null')}

    async def events(self, owner, identity, **query):
        from app.instances.events import listing
        await self.row(owner, identity, active=False)
        return await listing(self, identity, **query)

    async def archive(self, owner, identity, *, expected_revision=None):
        async with self.lock(identity):
            row = await self.row(owner, identity, active=False)
            if expected_revision is not None and row['revision'] != expected_revision:
                raise InstanceError('instance_changed_after_confirmation', 409)
            op = await one(self.db.conn, 'SELECT status FROM instance_operations WHERE id=?', (row['active_operation'],))
            if op and op['status'] in {'pending', 'running'}:
                raise InstanceError('stop_operation_before_archive', 409)
            if row['auth_mode'] == 'gateway' and row['status'] != 'archived':
                from app.instances.gateway import prepare
                item, protected = json.loads(row['public_json']), self.vault.open(row['protected'])
                if not item.get('archivePending'):
                    prepare(item, protected, enabled=False)
                # A stopped gateway lets no one in: archived now, its account disabled there when it
                # is enabled again (every instance is synced then).
                stopped = not (await self.gateway.config())[0]['enabled']
                if stopped:
                    item.pop('archivePending', None)
                else:
                    item['archivePending'] = True
                await self.gateway.persist(row, item, protected, archived=stopped)
                if stopped:
                    async with self.db.write_transaction(label='instance-archive-notifications') as conn:
                        await conn.execute("UPDATE instance_notifications SET status='suppressed',updated_ms=? WHERE instance_id=? AND status='pending'", (now(), identity))
                pending = not stopped
            else:
                pending = False
                async with self.db.write_transaction(label='instance-archive') as conn:
                    await conn.execute("UPDATE managed_instances SET status='archived',revision=revision+1 WHERE id=?", (identity,))
                    await conn.execute("UPDATE instance_notifications SET status='suppressed',updated_ms=? WHERE instance_id=? AND status='pending'", (now(), identity))
                self._mesh_changed(owner)
        if pending:
            return await self.gateway.sync(owner, identity)
        return await self.get(owner, identity)

    async def request(self, owner, identity, data, *, expected_revision=None):
        if set(data) - {'method', 'path', 'body'}:
            raise InstanceError('relay_fields_forbidden', 403)
        async with self.lock(identity):
            row = await self.row(owner, identity)
            if expected_revision is not None and row['revision'] != expected_revision:
                raise InstanceError('instance_changed_after_confirmation', 409)
            status, value = await network.request(self, row, data.get('method', 'GET'), data.get('path'), data.get('body'))
        return {'ok': True, 'status': status, 'data': value}

    async def verify(self, owner, identity):
        async with self.lock(identity):
            row = await self.row(owner, identity)
            check = await network.probe(self, row)
            await self._verified(row, check)
            if check['ok'] and row['active_operation']:
                # The owner's check settles an operation left unverified (no report, interrupted):
                # a restart does not take it for one still open.
                async with self.db.write_transaction(label='instance-verified-operation') as conn:
                    await conn.execute("UPDATE instance_operations SET status='completed',error='',verification_json=?,updated_ms=? "
                                       "WHERE id=? AND status IN ('needs_verification','interrupted')", (dumps(check), now(), row['active_operation']))
        return {**await self.get(owner, identity), 'verification': check}

    async def _verified(self, row, check, *, status=None):
        item = json.loads(row['public_json'])
        item.update(updatedAtMs=now(), lastError=check.get('error', ''), healthStatus=check.get('health', {}).get('status', 'unknown'), lastHealthAtMs=now(), verification=check)
        alive = check['identity']
        if not alive:
            # Reachable through the gateway still proves the container is running.
            evidence = network.child_evidence((check.get('gateway') or {}).get('status'), row['id'])
            if evidence['identity']:
                alive = True
                item['healthStatus'] = evidence['health']['status']
        if alive:
            item['lastSeenAtMs'] = now()
        state = status or ('ready' if check['ok'] else 'unreachable' if check['error'] == 'child_unreachable' and not alive else 'needs_verification')
        if state == 'ready' and check['ok']:
            item['deploymentAccepted'] = True
        elif status is not None:
            # Terminal/no-report uncertainty cannot be promoted by the next
            # periodic healthy probe as if deployment had been accepted.
            item['deploymentAccepted'] = False
        async with self.db.write_transaction(label='instance-verified') as conn:
            await conn.execute('UPDATE managed_instances SET status=?,public_json=? WHERE id=? AND revision=? AND status!=?',
                               (state, dumps(item), row['id'], row['revision'], 'archived'))

    async def start_operation(self, owner, identity, kind, data, *, expected_revision=None, origin=('', '')):
        """origin: (conversation, root turn) of the turn that asked for it, so the operation's turn keeps that turn's origin."""
        if kind not in {'deployment', 'maintenance'} or set(data) - {'instructions', 'model'}:
            raise InstanceError('invalid_operation')
        instructions, model = data.get('instructions', ''), data.get('model', '')
        if not isinstance(instructions, str) or len(instructions) > 20000 or not isinstance(model, str):
            raise InstanceError('invalid_operation')
        if kind == 'maintenance' and not instructions.strip():
            raise InstanceError('maintenance_scope_required')
        if model and not self.host.config.models.resolve(model):
            raise InstanceError('unknown_model')
        async with self.lock(identity):
            row = await self.row(owner, identity)
            if expected_revision is not None and row['revision'] != expected_revision:
                raise InstanceError('instance_changed_after_confirmation', 409)
            old = await one(self.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (row['active_operation'],))
            if old and old['status'] in {'pending', 'running'}:
                raise InstanceError('operation_running', 409)
            op_id, conv_id, root = uid(), uid(), uid()
            item = self.public(row)
            if item.get('archivePending'):
                raise InstanceError('gateway_revocation_pending', 409)
            folder = await self._folder(owner)
            conversation = await self.host._create_web_conversation(owner, title=('部署 · ' if kind == 'deployment' else '维护 · ')+item['name'],
                model=model, conversation_uuid=conv_id, origin_kind=kind, origin_ref=op_id, folder_uuid=folder)
            protected = self.vault.open(row['protected'])
            material = {**protected, 'instanceId': identity, 'operationId': op_id, 'scope': instructions,
                        'target': await self._target_summary(owner, item),
                        'imageProvenance': '用户镜像必须核实可取得；没有现成镜像时，先确认同仓源码版本，在目标构建同仓 Dockerfile，或使用另行授权的镜像传输。不能假设官方镜像已发布；现有 ZIP 不是完整构建上下文。',
                        'constraints': '仅此目标上的 Docker 子实例；/workspace 是唯一持久卷；不得挂载 Docker socket、主控密钥或其他租户目录。密钥真实且已授权传给模型，不要回显/复制进普通工具参数或日志，使用 InstanceControl deliver 受控交付。'}
            bundle = await self.vault.bundle(material, conv_id, op_id)
            async with self.db.write_transaction(label='instance-operation') as conn:
                await conn.execute('INSERT INTO instance_operations(id,instance_id,owner,kind,status,conversation_uuid,chat_id,root,bundle,'
                                   'origin_conversation,origin_root,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (op_id, identity, owner, kind, 'pending', conv_id, conversation['internal_chat_id'], root, bundle,
                                    *origin, now(), now()))
                item.update(conversationUuid=conv_id, updatedAtMs=now())
                await conn.execute("UPDATE managed_instances SET active_operation=?,status='deploying',public_json=?,revision=revision+1 WHERE id=?", (op_id, dumps(item), identity))
                op = await one(conn, 'SELECT * FROM instance_operations WHERE id=?', (op_id,))
            from app.instances.runtime import execute
            task = asyncio.create_task(execute(self, op_id, conversation, root, instructions), name='instance:'+op_id)
            self.tasks[op_id] = task
            self.host.runs.register(conversation['internal_chat_id'], task)
            task.add_done_callback(lambda done: self.tasks.pop(op_id, None))
        return {'ok': True, 'operation': self.operation(op), 'conversationUuid': conv_id}

    async def prepare_turn(self, conversation, root, *, automatic=False):
        """Origin is never inspected. Only our exact server-side conversation link grants scope."""
        if automatic or not conversation or not root or self.managed:
            return None
        op = await one(self.db.conn, 'SELECT * FROM instance_operations WHERE conversation_uuid=? AND chat_id=? AND owner=?',
                       (conversation['conversation_uuid'], conversation['internal_chat_id'], conversation['owner_chat_id']))
        if not op:
            return None
        async with self.lock(op['instance_id']):
            op = await one(self.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (op['id'],))
            row = await self.row(op['owner'], op['instance_id'], active=False)
            if row['status'] == 'archived' or row['active_operation'] != op['id']:
                return None
            current = await one(self.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=? AND internal_chat_id=? AND owner_chat_id=?',
                                (op['conversation_uuid'], op['chat_id'], op['owner']))
            if not current:
                return None
            if op['status'] == 'running':
                raise InstanceError('operation_running', 409)
            bundle = await self.vault.refresh(row, op) if op['revision'] else op['bundle']
            # A question after it completed changes nothing until the turn acts (acted()).
            resumed = op['status'] == 'completed'
            async with self.db.write_transaction(label='instance-bind') as conn:
                await conn.execute("UPDATE instance_operations SET revision=revision+1,status='running',root=?,run_id='',report_json=NULL,"
                                   "verification_json=CASE WHEN ? THEN verification_json END,error='',bundle=?,resumed=?,updated_ms=? WHERE id=?",
                                   (root, resumed, bundle, 'completed' if resumed else '', now(), op['id']))
                if not resumed:
                    await conn.execute("UPDATE managed_instances SET status='deploying' WHERE id=?", (row['id'],))
                # The read connection may not see this commit yet.
                op = await one(conn, 'SELECT * FROM instance_operations WHERE id=?', (op['id'],))
            return {**op, 'scrub': Scrubber(secret_values(self.vault.open(row['protected'])))}

    async def acted(self, op):
        """The turn of a resumed operation does something on the server: the operation is open again
        and has to report like any other."""
        if not op.get('resumed'):
            return
        async with self.db.write_transaction(label='instance-resume') as conn:
            cursor = await conn.execute("UPDATE instance_operations SET resumed='',verification_json=NULL WHERE id=? AND resumed!=''", (op['id'],))
            if cursor.rowcount:
                await conn.execute("UPDATE managed_instances SET status='deploying' WHERE id=?", (op['instance_id'],))
        op['resumed'] = ''

    async def bind_run(self, binding, run_id):
        async with self.db.write_transaction(label='instance-run') as conn:
            cursor = await conn.execute("UPDATE instance_operations SET run_id=? WHERE id=? AND revision=? AND root=? AND status='running' AND run_id=''", (run_id, binding['id'], binding['revision'], binding['root']))
            if cursor.rowcount != 1:
                raise InstanceError('stale_operation', 409)
        binding['run_id'] = run_id

    async def authority(self, ctx, *, expected_revision=None):
        self.require_controller()
        if ctx.agent_session_uuid or ctx.task_uuid or not ctx.run_id or not ctx.conversation_uuid:
            raise InstanceError('bound_controller_run_required', 403)
        op = await one(self.db.conn, "SELECT o.* FROM instance_operations o JOIN managed_instances i ON i.active_operation=o.id JOIN web_conversations c ON c.conversation_uuid=o.conversation_uuid AND c.internal_chat_id=o.chat_id AND c.owner_chat_id=o.owner WHERE o.conversation_uuid=? AND o.chat_id=? AND o.root=? AND o.run_id=? AND o.status='running' AND i.status!='archived'",
                       (ctx.conversation_uuid, ctx.chat_id, ctx.run_root_turn_uuid, ctx.run_id))
        if not op:
            raise InstanceError('bound_controller_run_required', 403)
        if expected_revision is not None and expected_revision != op['revision']:
            raise InstanceError('stale_operation_revision', 409)
        return op

    async def report(self, ctx, data):
        if type(data.get('revision')) is not int or data.get('outcome') not in {'completed', 'failed', 'needs_verification'}:
            raise InstanceError('report_revision_and_outcome_required')
        op = await self.authority(ctx, expected_revision=data['revision'])
        async with self.lock(op['instance_id']):
            op = await self.authority(ctx, expected_revision=data['revision'])
            row = await self.row(op['owner'], op['instance_id'])
            if op['report_json']:
                raise InstanceError('report_already_filed_for_revision', 409)
            scrub = Scrubber(secret_values(self.vault.open(row['protected'])))
            summary = data.get('summary', '')
            if not isinstance(summary, str) or len(summary) > 12000:
                raise InstanceError('invalid_report')
            report = {'outcome': data['outcome'], 'summary': scrub(summary), 'atMs': now()}
            check = await network.probe(self, row)
            async with self.db.write_transaction(label='instance-report') as conn:
                await conn.execute('UPDATE instance_operations SET report_json=?,verification_json=?,updated_ms=? WHERE id=? AND revision=? AND run_id=?',
                                   (dumps(report), dumps(check), now(), op['id'], op['revision'], ctx.run_id))
            # The run remains live until terminal; no old turn may report after it.
            await self._verified(row, check)
        return {'ok': True, 'operationId': op['id'], 'revision': op['revision'], 'verification': check}

    async def finish(self, identity, revision, root, reason, *, recovering=False):
        op = await one(self.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (identity,))
        if not op:
            return
        async with self.lock(op['instance_id']):
            op = await one(self.db.conn, 'SELECT * FROM instance_operations WHERE id=?', (identity,))
            if op['revision'] != revision or op['root'] != root or op['status'] not in ({'pending', 'running', 'needs_verification', 'interrupted'} if recovering else {'pending', 'running'}):
                return
            row = await self.row(op['owner'], op['instance_id'], active=False)
            if row['active_operation'] != identity or row['status'] == 'archived':
                return
            report = json.loads(op['report_json'] or 'null')
            if op['resumed'] and not report and op['status'] in {'pending', 'running'}:
                # Only talked to: it is completed as it was, nothing sent.
                async with self.db.write_transaction(label='instance-resumed-done') as conn:
                    await conn.execute("UPDATE instance_operations SET status='completed',error='',resumed='',updated_ms=? WHERE id=? AND revision=?",
                                       (now(), identity, revision))
                return
            # Terminal first: whatever the probe below meets, the operation no longer
            # holds the instance (archive, revocation and new operations wait for it).
            if recovering and op['status'] in {'pending', 'running'}:
                async with self.db.write_transaction(label='instance-recovery-interrupted') as conn:
                    await conn.execute("UPDATE instance_operations SET status='interrupted',error='run_interrupted',updated_ms=? WHERE id=? AND revision=?", (now(), identity, revision))
            elif not recovering:
                interim = 'interrupted' if reason != 'completed' else 'failed' if report and report['outcome'] == 'failed' else 'needs_verification'
                async with self.db.write_transaction(label='instance-terminal-interim') as conn:
                    await conn.execute('UPDATE instance_operations SET status=?,error=?,updated_ms=? WHERE id=? AND revision=?',
                                       (interim, 'run_'+reason if reason != 'completed' else 'verification_pending', now(), identity, revision))
            # Always recheck; a report is evidence, never readiness authority.
            try:
                check = await network.probe(self, row)
            except Exception:
                check = network.failed_check('verification_failed')
            success = bool(report and report['outcome'] == 'completed' and check['ok'] and reason == 'completed')
            status = 'completed' if success else 'interrupted' if reason != 'completed' else 'failed' if report and report['outcome'] == 'failed' else 'needs_verification'
            # A recovered *reported* result may complete verification, not execution.
            if recovering and report and report['outcome'] == 'completed' and check['ok']:
                status = 'completed'
            if recovering and op['status'] in {'needs_verification', 'interrupted'} and status != 'completed':
                status = op['status']
            error = '' if status == 'completed' else (op['error'] or check['error']) if recovering and op['status'] in {'needs_verification', 'interrupted'} else 'run_'+reason if reason != 'completed' else 'no_report' if not report else check['error'] or 'reported_'+report['outcome']
            async with self.db.write_transaction(label='instance-terminal') as conn:
                await conn.execute('UPDATE instance_operations SET status=?,verification_json=?,error=?,updated_ms=? WHERE id=? AND revision=?', (status, dumps(check), error, now(), identity, revision))
                if not recovering or op['status'] in {'pending', 'running'} or status != op['status']:
                    suffix = ':recovery:'+status if recovering and op['status'] in {'needs_verification', 'interrupted'} else ''
                    await self._enqueue(conn, f'operation:{identity}:{revision}'+suffix, row,
                                        f'实例{quoted(self.public(row)["name"])}的{OPERATION_KINDS.get(op["kind"], op["kind"])}{OPERATION_ENDS.get(status, status)}',
                                        op['conversation_uuid'])
            await self._verified(row, check, status='ready' if status == 'completed' else 'needs_verification')

    # ----- key rotation and network migration -----
    async def _persist(self, row, item, protected):
        async with self.db.write_transaction(label='instance-keys') as conn:
            await conn.execute('UPDATE managed_instances SET public_json=?,protected=?,revision=revision+1 WHERE id=?',
                               (dumps(item), self.vault.seal(protected), row['id']))

    async def _own_workspace(self, form, identity, chosen):
        """A data directory, and the container named after it, no other instance on the server has.
        The default one (named after the account, 'instance' for one in other scripts) takes a
        suffix of this instance's id when taken, also by an archived instance, whose data is still
        there; a chosen directory must not overlap any other instance's data."""
        from app.instances.remote import container_name
        taken = set()
        for row in await many(self.db.conn, 'SELECT id,status,public_json FROM managed_instances WHERE id!=?', (identity,)):
            other = json.loads(row['public_json'])
            if other.get('serverId') == form['serverId'] and other.get('workspacePath'):
                taken |= {('path', other['workspacePath']), ('name', container_name(other))}

        def resolved(path):
            return Path(os.path.realpath(path) if form['serverId'] == 'local' else os.path.normpath(path))

        def clash(path):
            candidate = resolved(path)
            return (any(candidate == resolved(other) or candidate in resolved(other).parents or resolved(other) in candidate.parents
                        for kind, other in taken if kind == 'path')
                    or ('name', container_name({'workspacePath': path})) in taken)
        if not clash(form['workspacePath']):
            return
        if chosen:
            raise InstanceError('instance_workspace_in_use', 409)
        form['workspacePath'] = f"{form['workspacePath']}-{identity.replace('-', '')[:6]}"
        if clash(form['workspacePath']):
            raise InstanceError('instance_workspace_in_use', 409)

    async def _delivered_image(self, owner, row, item):
        """The image the instance was delivered with, '' when it was not delivered. A record a form
        save emptied (before saves kept it) takes it back from the compose.json on its server."""
        if item.get('image') or row['status'] == 'draft':
            return item.get('image') or ''
        from app.instances.remote import deploy_directory
        target = await self.servers.target(owner, item['serverId'], nodes=True)
        path = deploy_directory(item) + '/compose.json'
        result = await self.servers.run(target, f'cat {shlex.quote(path)} 2>/dev/null || true', timeout=30)
        try:
            image = json.loads(result.get('stdout') or '{}')['services']['openbear']['image']
        except (ValueError, KeyError, TypeError):
            image = ''
        if isinstance(image, str) and image:
            item['image'] = image
            return image
        if (item.get('deploymentAccepted') or item.get('lastSeenAtMs') or row['status'] in {'ready', 'unreachable'}
                or (item.get('verification') or {}).get('identity')):
            raise InstanceError('instance_image_unknown', 409)
        return ''

    async def _idle(self, row):
        op = await one(self.db.conn, 'SELECT status FROM instance_operations WHERE id=?', (row['active_operation'],))
        return not (op and op['status'] in {'pending', 'running'})

    async def rotate_keys(self, owner, identity, *, queue_if_busy=False):
        """New business and management keys. The new pair is stored first, then set on the
        instance's volume (offline set-auth, which also revokes its sessions) and in its
        container, then sent to the gateway, which drops the instance's login sessions on a
        changed key. An instance that cannot be reached keeps working with the old pair and
        is retried (rotation_upkeep) until it takes the new one."""
        async with self.lock(identity):
            row = await self.row(owner, identity)
            if row['auth_mode'] != 'gateway':
                raise InstanceError('instance_gateway_auth_required', 409)
            if not queue_if_busy and not await self._idle(row):
                raise InstanceError('operation_running', 409)
            item, protected = json.loads(row['public_json']), self.vault.open(row['protected'])
            # A pending pair may already be on the volume: it is finished, not replaced.
            if not protected.get('rotation'):
                protected['rotation'] = {'businessKey': secrets.token_urlsafe(48), 'managementKey': secrets.token_urlsafe(48)}
                item['rotation'] = {'status': 'pending', 'requestedAtMs': now(), 'attempts': 0, 'nextAtMs': 0, 'error': ''}
                await self._persist(row, item, protected)
        return await self.apply_rotation(owner, identity)

    async def apply_rotation(self, owner, identity):
        async with self.lock(identity):
            row = await self.row(owner, identity, active=False)
            item, protected = json.loads(row['public_json']), self.vault.open(row['protected'])
            keys = protected.get('rotation')
            if not keys or row['status'] == 'archived':
                return {**await self.get(owner, identity), 'rotated': False}
            from app.instances.gateway import prepare
            from app.instances.remote import reconfigure
            error = ''
            # One key per step, each a single write on the volume that any deployed image's
            # maintenance accepts; a step repeated after a lost result changes nothing.
            steps = (('businessKey', 'set-auth', {'authMode': 'gateway', 'gatewayKey': keys['businessKey']}),
                     ('managementKey', 'rotate-identity', {'managementKey': keys['managementKey']}))
            for field, action, payload in steps:
                if protected[field] == keys[field]:
                    continue
                if not await self._idle(row):
                    error = 'operation_running'
                    break
                try:
                    delivered = await self._delivered_image(owner, row, item)
                except InstanceError as exc:
                    error = exc.error
                    break
                if delivered:
                    # Delivered: volume and container take the key. Otherwise the first
                    # deployment simply starts with it.
                    command, stdin = reconfigure(row, {**protected, field: keys[field]}, item, maintenance=[(action, payload)])
                    try:
                        target = await self.servers.target(owner, item['serverId'], nodes=True)
                        result = await self.servers.run(target, command, stdin=stdin, timeout=300)
                        error = '' if result['ok'] else 'instance_rotation_failed'
                    except InstanceError as exc:
                        error = exc.error
                    if error:
                        break
                protected[field] = keys[field]
                if field == 'businessKey':
                    # The gateway follows (sync below), dropping this instance's login sessions.
                    prepare(item, protected, enabled=not item.get('archivePending', False))
                await self._persist(row, item, protected)
            if error:
                state = item.get('rotation') or {}
                attempts = int(state.get('attempts', 0)) + 1
                item['rotation'] = {**state, 'status': 'pending', 'attempts': attempts, 'error': error,
                                    'nextAtMs': now() + min(ROTATION_RETRY_MAX_MS, ROTATION_RETRY_MS * 2 ** min(attempts, 10))}
            else:
                protected.pop('rotation')
                item.pop('rotation', None)
                item['keysRotatedAtMs'] = now()
            await self._persist(row, item, protected)
        synced = await self.gateway.sync(owner, identity)
        return {**synced, 'rotated': not error, **({'error': error} if error else {})}

    async def rotation_upkeep(self):
        """Retry pending rotations whose instance may be back."""
        for row in await many(self.db.conn, "SELECT id,owner,public_json FROM managed_instances WHERE status!='archived'"):
            state = json.loads(row['public_json']).get('rotation')
            if state and state.get('status') == 'pending' and state.get('nextAtMs', 0) <= now():
                with contextlib.suppress(Exception):
                    await self.apply_rotation(row['owner'], row['id'])

    async def network_migration(self, owner, row):
        """Whether migrate_network applies to this instance: None when it does not (already placed,
        archived, a fixed address, a port that is not open to all, or an SSH-only server, which
        keeps server address + port), else {'blockers': [...]}, empty when it can run now, else
        what must change first."""
        from app.instances.remote import binds, mesh_bind
        item = json.loads(row['public_json'])
        placed = item.get('network')
        if row['status'] == 'archived':
            return None
        moved = False
        if placed and not placed.get('pending'):
            # Placed on a mesh address its server no longer has (a new prefix, another address).
            bound = mesh_bind(item)
            if not bound or await self.servers.mesh_address(owner, item['serverId']) == bound:
                return None
            moved = True
        elif placed:
            return {'pending': True, 'blockers': []}
        elif '0.0.0.0' not in binds(item) or self.vault.open(row['protected'])['form'].get('managementUrl'):
            return None
        blockers = []
        if item['serverId'] != 'local':
            reason = await self.servers.http_blocker(owner, item['serverId'])
            if reason == 'node_missing':
                return None
            if reason:
                blockers.append(reason)
        gateway, _ = await self.gateway.config()
        gateway_server = gateway['serverId'] or 'local'
        if gateway_server != item['serverId']:
            # The same rule as _placement, said per server.
            if not await self.servers.mesh_address(owner, item['serverId']):
                blockers.append('server_mesh_required')
            if not await self.servers.mesh_address(owner, gateway_server):
                blockers.append('gateway_mesh_required')
        return {'pending': False, 'blockers': blockers, **({'moved': True} if moved else {})}

    async def _migration_or_none(self, owner, row):
        # A display hint: unreadable protected storage must not break listing the instances.
        try:
            return await self.network_migration(owner, row)
        except InstanceError:
            return None

    async def migrate_network(self, owner, identity):
        """Maintenance for an instance deployed before loopback placement: its port listens on
        loopback (plus the mesh address when the gateway is on another server), the controller
        reaches it through the node and the gateway at its new address. The record moves first:
        the old 0.0.0.0 binding also answers there until the container is recreated."""
        async with self.lock(identity):
            row = await self.row(owner, identity)
            if not await self._idle(row):
                raise InstanceError('operation_running', 409)
            item, protected = json.loads(row['public_json']), self.vault.open(row['protected'])
            from app.instances.gateway import prepare
            from app.instances.remote import boot_unit, mesh_bind, reconfigure
            # Placed on a mesh address its server no longer has: placed again, at the one it has now.
            moved = bool(item.get('network') and not item['network'].get('pending') and mesh_bind(item)
                         and await self.servers.mesh_address(owner, item['serverId']) != mesh_bind(item))
            if item.get('network') and not item['network'].get('pending') and not moved:
                return {**await self.get(owner, identity), 'migrated': False}
            if not item.get('network') or moved:
                if protected['form'].get('managementUrl') and not moved:
                    raise InstanceError('instance_address_fixed', 409)
                address, placed = await self._placement(owner, item['serverId'], item['port'])
                if not placed:
                    raise InstanceError('node_http_required', 409)
                # pending until the container listens there; running this again finishes it.
                item.update(managementUrl=address, network={**placed, 'pending': True}, updatedAtMs=now())
                prepare(item, protected, enabled=not item.get('archivePending', False))
                await self._persist(row, item, protected)
                # The gateway reaches it on the mesh address once the container is recreated.
                self._mesh_changed(owner)
            if await self._delivered_image(owner, row, item):
                target = await self.servers.target(owner, item['serverId'], nodes=True)
                if mesh_bind(item):
                    _, install, text = boot_unit(item)
                    if not (await self.servers.run(target, install, stdin=text, timeout=60, root=True))['ok']:
                        raise InstanceError('instance_boot_unit_failed', 502)
                command, stdin = reconfigure(row, protected, item)
                if not (await self.servers.run(target, command, stdin=stdin, timeout=300))['ok']:
                    raise InstanceError('instance_reconfigure_failed', 502)
            item['network'].pop('pending', None)
            await self._persist(row, item, protected)
        return {**await self.gateway.sync(owner, identity), 'migrated': True}

    async def _target_summary(self, owner, item):
        server = await self.servers.get(owner, item['serverId'])
        return {'server': server['name'], 'local': server.get('local', False), 'host': server.get('host', ''),
                # Node first, SSH while the node is offline; each command result names the channel used.
                **await self.servers.channel(owner, item['serverId']),
                'port': item['port'], 'managementUrl': item['managementUrl'], 'workspacePath': item['workspacePath'],
                'image': item.get('image') or '与主控同版本（按需在目标上从源码构建）'}

    async def _folder(self, owner):
        """Every deployment/maintenance session lives in one folder, recreated if deleted."""
        from app.context.prompt_policy import read_json, write_json
        key = f'instance_deployment_folder:{owner}'
        async with self.host._conversation_tree_lock:
            async with self.db.write_transaction(label='instance-folder') as conn:
                folder = await read_json(conn, key)
                if folder and await one(conn, 'SELECT 1 FROM web_conversation_folders WHERE owner_chat_id=? AND folder_uuid=?', (owner, folder)):
                    return folder
                folder = uid()
                taken = {r['name'] for r in await many(conn, "SELECT name FROM web_conversation_folders WHERE owner_chat_id=? AND parent_uuid=''", (owner,))}
                name = next(n for n in (FOLDER_NAME, *(f'{FOLDER_NAME} {i}' for i in range(2, 100))) if n not in taken)
                stamp = int(time.time())
                await conn.execute('''INSERT INTO web_conversation_folders
                    (folder_uuid,owner_chat_id,parent_uuid,name,display_order,created_at,updated_at,prompt_markdown)
                    VALUES (?,?,?,?,?,?,?,?)''', (folder, owner, '', name, 1e12, stamp, stamp, FOLDER_PROMPT))
                await write_json(conn, key, folder)
                return folder

    async def folder(self, owner):
        return {'ok': True, 'folderUuid': await self._folder(owner)}

    async def _enqueue(self, conn, key, row, text, conversation=''):
        await conn.execute('INSERT OR IGNORE INTO instance_notifications(id,instance_id,owner,conversation_uuid,text,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?)', (key, row['id'], row['owner'], conversation, text, now(), now()))

    async def _worker(self):
        await wait_for_startup(self)
        while True:
            try:
                await self.notify_pending()
            except Exception:
                pass  # Durable retry state owns delivery, never rerun deployments.
            await asyncio.sleep(5)

    async def notify_pending(self):
        from app.instances.notifications import deliver
        async with self.lock('notifications'):
            for row in await many(self.db.conn, "SELECT * FROM instance_notifications WHERE status='pending' AND next_ms<=? LIMIT 50", (now(),)):
                try:
                    state = await deliver(self, row)
                except Exception:
                    state = 'failed' if row['attempts'] >= 7 else 'pending'
                async with self.db.write_transaction(label='instance-notification') as conn:
                    await conn.execute('UPDATE instance_notifications SET status=?,attempts=attempts+1,next_ms=?,updated_ms=? WHERE id=?',
                                       (state, now()+min(3600000, 5000 * 2**row['attempts']), now(), row['id']))
