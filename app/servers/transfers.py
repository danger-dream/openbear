"""Path-to-path transfers: byte payloads stay between the controller and its node."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path

from app.instances.contracts import InstanceError

CHUNK = 1024 * 1024
TERMINAL = {'done', 'failed', 'cancelled', 'unknown'}
DIGEST = re.compile(r'^[0-9a-f]{64}$')


def now_ms():
    return int(time.time() * 1000)


def local_path(value):
    if not isinstance(value, str) or not value or '\0' in value or not Path(value).is_absolute():
        raise InstanceError('absolute_local_path_required')
    return Path(value)


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


async def disk_io(fn, *args):
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The descriptor must stay open until its worker has stopped using it.
        with contextlib.suppress(Exception):
            await task
        raise


class Transfers:
    def __init__(self, service):
        self.service = service
        self.jobs = {}

    async def start(self, owner, server_id, direction, params):
        local = local_path(params.get('localPath'))
        remote = params.get('remotePath')
        if not isinstance(remote, str) or '\0' in remote or not (remote.startswith('/') or re.match(r'^[A-Za-z]:[\\/]', remote)):
            raise InstanceError('absolute_remote_path_required')
        for flag in ('overwrite', 'mkdirs'):
            if type(params.get(flag, False)) is not bool:
                raise InstanceError('invalid_parameters')
        mode = params.get('mode', '')
        if not isinstance(mode, str) or (mode and not re.fullmatch('[0-7]{3,4}', mode)):
            raise InstanceError('invalid_mode')
        await self.service.registry.get(owner, server_id)
        session = self.service.hub.sessions.get(server_id)
        if session is None:
            raise InstanceError('node_offline', 409)
        if not session.caps.get('exec'):
            raise InstanceError('capability_disabled', 403)
        if 'fs.transfer' not in session.features:
            raise InstanceError('node_update_required', 409)
        if direction == 'upload':
            try:
                info = local.stat()
            except OSError as exc:
                raise InstanceError('local_file_unavailable') from exc
            if not stat.S_ISREG(info.st_mode):
                raise InstanceError('not_regular_file')
            total = info.st_size
        else:
            if local.is_symlink() or (local.exists() and not local.is_file()):
                raise InstanceError('not_regular_file')
            if local.exists() and not params.get('overwrite', False):
                raise InstanceError('file_exists', 409)
            total = None
        # Keep active work; only completed receipts age out.
        for key, job in list(self.jobs.items()):
            if job['state'] in TERMINAL and now_ms() - job['endedAtMs'] > 86400000:
                del self.jobs[key]
        if sum(j['state'] not in TERMINAL for j in self.jobs.values()) >= 8:
            raise InstanceError('too_many_transfers', 409)
        key = secrets.token_hex(12)
        job = {'id': key, 'owner': owner, 'serverId': server_id, 'direction': direction,
               'localPath': str(local), 'remotePath': remote, 'overwrite': params.get('overwrite', False),
               'mkdirs': params.get('mkdirs', False), 'mode': mode, 'state': 'queued', 'bytes': 0, 'total': total,
               'sha256': '', 'verified': False, 'error': '', 'cleanupPending': False,
               'createdAtMs': now_ms(), 'endedAtMs': None, 'cancel': False}
        self.jobs[key] = job
        job['task'] = self.service.spawn(self.run(job))
        return {'ok': True, 'transfer': self.view(job)}

    def view(self, job):
        result = {k: v for k, v in job.items() if k not in {'owner', 'task', 'cancel', 'mkdirs', 'mode'}}
        result['cancellable'] = job['state'] not in TERMINAL | {'committing', 'cancelling'}
        result['effectState'] = 'unknown' if job['state'] == 'unknown' else 'observed' if job['state'] == 'done' else 'pending' if job['state'] not in TERMINAL else 'not_committed'
        if job['direction'] == 'download' and job['state'] == 'done':
            workspace = getattr(self.service.host, 'workspace_dir', None)
            if workspace:
                try:
                    relative = Path(job['localPath']).resolve().relative_to(Path(workspace).resolve() / 'artifacts')
                    result['artifact'] = (Path('workspace/artifacts') / relative).as_posix()
                except ValueError:
                    pass
        return result

    def get(self, owner, server_id, key):
        job = self.jobs.get(key)
        if not job or job['owner'] != owner or job['serverId'] != server_id:
            raise InstanceError('transfer_not_found', 404)
        return job

    async def status(self, owner, server_id, key, wait=0):
        job = self.get(owner, server_id, key)
        if type(wait) is not int or not 0 <= wait <= 60:
            raise InstanceError('invalid_wait')
        if wait and job['state'] not in TERMINAL:
            await asyncio.wait({job['task']}, timeout=wait)
        return {'ok': True, 'transfer': self.view(job)}

    async def cancel(self, owner, server_id, key):
        job = self.get(owner, server_id, key)
        accepted = job['state'] not in TERMINAL | {'committing'}
        if accepted:
            job['cancel'] = True
            job['state'] = 'cancelling'
        return {'ok': True, 'cancelRequested': accepted, 'transfer': self.view(job)}

    async def close(self):
        tasks = {j['task'] for j in self.jobs.values() if j['state'] not in TERMINAL}
        for job in self.jobs.values():
            job['cancel'] = True
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def check_cancel(self, job):
        if job['cancel']:
            raise asyncio.CancelledError

    async def call(self, job, method, params, timeout=60):
        return await self.service._call_newer(job['owner'], job['serverId'], 'fs.transfer', method, params, timeout=timeout)

    async def hash_remote(self, job):
        value = await self.call(job, 'fs.hash', {'path': job['remotePath']}, timeout=300)
        if type(value.get('size')) is not int or value['size'] < 0 or not DIGEST.fullmatch(str(value.get('sha256', ''))):
            raise InstanceError('invalid_file_hash', 502)
        return value

    async def run(self, job):
        try:
            self.check_cancel(job)
            if job['direction'] == 'upload':
                await self.upload(job)
            else:
                await self.download(job)
            job['state'] = 'done'
        except asyncio.CancelledError:
            job['error'] = 'transfer_interrupted' if job['state'] == 'committing' else 'transfer_cancelled'
            job['state'] = 'unknown' if job['state'] == 'committing' else 'cancelled'
        except InstanceError as exc:
            job['error'] = exc.error
            job['state'] = 'unknown' if job['state'] == 'committing' and getattr(exc, 'effect_state', '') == 'unknown' else 'failed'
        except Exception:
            job['error'] = 'transfer_failed'
            job['state'] = 'unknown' if job['state'] == 'committing' else 'failed'
        finally:
            if job['direction'] == 'upload' and job['state'] != 'done':
                try:
                    await self.call(job, 'fs.cancel', {'path': job['remotePath'], 'upload': job['id']}, timeout=10)
                except (Exception, asyncio.CancelledError):
                    job['cleanupPending'] = True
            job['endedAtMs'] = now_ms()
            with contextlib.suppress(Exception):
                await self.service._log(job['owner'], job['serverId'], 'ai',
                                        f"文件传输 {job['direction']} {job['remotePath']}（{job['bytes']} 字节）", job['createdAtMs'],
                                        {'exitCode': 0 if job['state'] == 'done' else 1, 'outcome': job['state']})

    async def upload(self, job):
        local = Path(job['localPath'])
        # Nonblocking open also rejects a source replaced with a FIFO after preflight.
        fd = os.open(local, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise InstanceError('not_regular_file')
            job.update(state='transferring', total=before.st_size)
            digest = hashlib.sha256()
            while True:
                self.check_cancel(job)
                data = await disk_io(source.read, CHUNK)
                if not data:
                    break
                if job['bytes'] + len(data) > job['total']:
                    raise InstanceError('local_file_changed')
                digest.update(data)
                await self.call(job, 'fs.transfer', {'path': job['remotePath'], 'upload': job['id'],
                               'offset': job['bytes'], 'data': base64.b64encode(data).decode(), 'done': False,
                               'noReplace': not job['overwrite'], 'mkdirs': job['mkdirs'], 'mode': job['mode']})
                job['bytes'] += len(data)
            if fingerprint(os.fstat(source.fileno())) != fingerprint(before) or job['bytes'] != before.st_size:
                raise InstanceError('local_file_changed')
        self.check_cancel(job)
        job.update(state='committing', sha256=digest.hexdigest())
        result = await self.call(job, 'fs.transfer', {'path': job['remotePath'], 'upload': job['id'],
                                 'offset': job['bytes'], 'data': '', 'done': True, 'size': job['bytes'], 'sha256': job['sha256'],
                                 'noReplace': not job['overwrite'], 'mkdirs': job['mkdirs'], 'mode': job['mode']}, timeout=300)
        if result.get('done') is not True or result.get('size') != job['bytes'] or result.get('sha256') != job['sha256']:
            error = InstanceError('invalid_commit_receipt', 502)
            error.effect_state = 'unknown'
            raise error
        job['verified'] = True

    async def download(self, job):
        job['state'] = 'checking'
        expected = await self.hash_remote(job)
        self.check_cancel(job)
        job.update(total=expected['size'], sha256=expected['sha256'], state='transferring')
        target = Path(job['localPath'])
        if job['mkdirs']:
            target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise InstanceError('not_regular_file')
        prior = target.stat() if target.exists() else None
        if prior and not job['overwrite']:
            raise InstanceError('file_exists', 409)
        fd, name = tempfile.mkstemp(prefix=f'.{target.name}.obtransfer-', dir=target.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, 'wb') as output:
                digest = hashlib.sha256()
                while job['bytes'] < job['total']:
                    self.check_cancel(job)
                    part = await self.service.read_file(job['owner'], job['serverId'], job['remotePath'], job['bytes'], min(CHUNK, job['total'] - job['bytes']))
                    data = part.get('data')
                    if (not isinstance(data, bytes) or not data or len(data) > min(CHUNK, job['total'] - job['bytes'])
                            or part.get('offset') != job['bytes'] or part.get('read') != len(data) or part.get('size') != job['total']):
                        raise InstanceError('remote_file_changed', 409)
                    await disk_io(output.write, data)
                    digest.update(data)
                    job['bytes'] += len(data)
                if digest.hexdigest() != job['sha256']:
                    raise InstanceError('checksum_mismatch', 409)
                output.flush()
                await disk_io(os.fsync, output.fileno())
                if prior:
                    os.fchown(output.fileno(), prior.st_uid, prior.st_gid)
                    os.fchmod(output.fileno(), stat.S_IMODE(prior.st_mode))
            self.check_cancel(job)
            job['state'] = 'committing'
            if job['overwrite']:
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise InstanceError('not_regular_file')
                os.replace(temp, target)
            else:
                try:
                    os.link(temp, target)
                except FileExistsError as exc:
                    raise InstanceError('file_exists', 409) from exc
            job['verified'] = True
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                job.update(cleanupPending=True, temporaryPath=str(temp))
