"""Deployment delivery and fixed-target command execution for a bound operation."""
from __future__ import annotations

import fnmatch
import io
import json
import re
import shlex
import tarfile
from pathlib import Path, PurePosixPath

from app.instances.contracts import InstanceError

ROOT = Path(__file__).resolve().parents[2]
# Mirrors .dockerignore: only distributable inputs; never config, data or secrets.
BUILD_INPUTS = ('Dockerfile', 'pyproject.toml', 'uv.lock', 'README.md', 'app', 'prompts', 'web/dist',
                'scripts/container-entrypoint.sh')
EXCLUDED = ('*/__pycache__', '*/__pycache__/*', '*.pyc')
# Root keeps running the container; a fork bomb or runaway tool still cannot exhaust the host.
PIDS_LIMIT = 4096


def _source_context():
    """Same-version build context; web/dist is shipped prebuilt so no Node build runs remotely."""
    from app.servers.bench_packages import PackageError, source_files
    try:
        packages = source_files(ROOT / 'benchmark-package')
    except PackageError as exc:
        raise InstanceError('benchmark_package_invalid', 409) from exc
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name in BUILD_INPUTS:
            path = ROOT / name
            if not path.exists():
                if name == 'web/dist':
                    raise InstanceError('web_dist_missing', 409)
                continue
            def keep(info):
                return None if any(fnmatch.fnmatch(info.name, p) for p in EXCLUDED) else info
            archive.add(path, arcname=name, filter=keep)
        for path in packages:
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    return stream.getvalue()


def container_name(item):
    """Named after the data directory (e.g. /opt/openbear-node -> openbear-node),
    which is unique per server; never after the login account."""
    base = re.sub(r'[^a-z0-9_.-]+', '-', PurePosixPath(item['workspacePath']).name.lower()).strip('-.')[:60]
    return base if base.startswith('openbear') else 'openbear-' + (base or 'instance')


def deploy_directory(item):
    """Beside the data directory, not inside it: the data directory is mounted
    into the container and must not carry the build context."""
    data = PurePosixPath(item['workspacePath'])
    return str(data.parent / ('.deploy-' + data.name))


def binds(item):
    """Host addresses the instance port listens on. Placed instances (item.network) listen on
    loopback, plus their mesh address when the gateway is on another server; older records
    keep the rule they were deployed with."""
    placed = (item.get('network') or {}).get('bind')
    if placed:
        return placed
    return ['127.0.0.1' if item['managementUrl'].startswith(('http://127.0.0.1:', 'http://localhost:')) else '0.0.0.0']


def mesh_bind(item):
    return next((b for b in binds(item) if b not in {'127.0.0.1', '0.0.0.0'}), '')


def compose(row, protected, item, image):
    environment = {'OPENBEAR_INSTANCE_ROLE': 'managed', 'OPENBEAR_INSTANCE_ID': row['id'],
                   'OPENBEAR_MANAGEMENT_KEY': protected['managementKey'], 'OPENBEAR_WORKSPACE': '/workspace',
                   'OPENBEAR_CHILD_AUTH': 'gateway', 'OPENBEAR_GATEWAY_KEY': protected['businessKey']}
    # Compose interpolates "$"; escape so credentials reach the container unchanged.
    environment = {k: v.replace('$', '$$') for k, v in environment.items()}
    ports = [f'[{b}]:{item["port"]}:18961' if ':' in b else f'{b}:{item["port"]}:18961' for b in binds(item)]
    service = {'image': image, 'container_name': container_name(item),
               'restart': 'unless-stopped', 'environment': environment, 'ports': ports,
               'volumes': [{'type': 'bind', 'source': item['workspacePath'], 'target': '/workspace'}],
               'security_opt': ['no-new-privileges:true'], 'pids_limit': PIDS_LIMIT}
    if item.get('memoryLimitMb'):
        service['mem_limit'] = f'{item["memoryLimitMb"]}m'
    return {'services': {'openbear': service}}


def boot_unit(item):
    """(unit name, install command, unit text) for an instance listening on its mesh address.
    The mesh service brings that address up after boot, often after Docker has already
    tried, and failed, to bind the port; the unit waits for it, then starts the container."""
    name, address = container_name(item), mesh_bind(item)
    unit = f'openbear-instance-{name}.service'
    text = f"""[Unit]
Description=Start OpenBear instance {name} once mesh address {address} is up
After=docker.service openbear-mesh.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=oneshot
TimeoutStartSec=10min
ExecStart=/bin/sh -c 'until ip -o addr show | grep -qF " inet {address}/"; do sleep 2; done; exec docker start {name}'

[Install]
WantedBy=multi-user.target
"""
    command = (f'[ -d /run/systemd/system ] || {{ echo instance-needs-systemd >&2; exit 3; }}; umask 022; '
               f'cat > /etc/systemd/system/{unit} && systemctl daemon-reload && systemctl enable {unit} >/dev/null 2>&1')
    return unit, command, text.encode()


def _tar(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.mode, info.size = 0o600, len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def reconfigure(row, protected, item, *, maintenance=()):
    """(command, stdin) that rewrites compose.json from the record and recreates the container
    with it, data directory untouched. maintenance: (action, payload) pairs for the offline
    maintenance CLI, run first on the stopped container's volume; when one fails nothing is
    replaced and the old container starts again. A volume that never started has nothing
    to maintain: its first start takes the new values."""
    image, workspace, name = item['image'], item['workspacePath'], container_name(item)
    files = {'compose.json': json.dumps(compose(row, protected, item, image), ensure_ascii=False).encode()}
    for index, (action, data) in enumerate(maintenance):
        files[f'{index}-{action}.json'] = json.dumps(data).encode()
    q = shlex.quote
    # Looked up as root inside the image: the volume is not readable by every SSH user.
    started = (f'docker run --rm --network none --entrypoint test -v {q(workspace)}:/workspace {q(image)} '
               f'-f /workspace/.managed/identity.json >/dev/null 2>&1')
    # Docker answering first: without it (a login user outside the docker group) both checks below
    # would fail and the script end as if nothing needed doing.
    lines = ['set -e', 'umask 077', 'if ! docker info >/dev/null 2>&1; then echo instance-docker-unavailable >&2; exit 4; fi',
             f'd={q(deploy_directory(item))}', 'mkdir -p "$d"', 'tmp=$(mktemp -d "$d/.reconfigure-XXXXXX")',
             'trap \'rm -rf "$tmp"\' EXIT', 'tar -xf - -C "$tmp"']
    if maintenance:
        steps = ' && '.join(f'docker run --rm -i --network none -v {q(workspace)}:/workspace {q(image)} '
                            f'python -m app.managed.maintenance --workspace /workspace {action} < "$tmp/{index}-{action}.json"'
                            for index, (action, _) in enumerate(maintenance))
        lines += [f'if {started}; then', f'  docker stop {name} >/dev/null 2>&1 || true',
                  f'  if ! {{ {steps}; }}; then', f'    docker start {name} >/dev/null 2>&1 || true',
                  '    echo instance-maintenance-failed >&2', '    exit 3', '  fi', 'fi']
    lines += ['mv "$tmp/compose.json" "$d/compose.json"',
              f'if {started} || docker inspect {name} >/dev/null 2>&1; then docker compose -f "$d/compose.json" up -d; fi']
    return '\n'.join(lines), _tar(files)


def deployment_archive(row, protected, item, params):
    """Generate real secrets in memory, never in model arguments or tool output."""
    from app import installed_version
    image = params.get('image') or item.get('image') or f'openbear:{installed_version()}'
    if not isinstance(image, str) or len(image) > 512 or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/@-]*', image):
        raise InstanceError('invalid_image', 409)
    if not protected.get('businessKey'):
        raise InstanceError('gateway_sync_required_before_new_deployment', 409)
    files = {'compose.json': json.dumps(compose(row, protected, item, image), ensure_ascii=False).encode()}
    if params.get('source', not item.get('image')):
        files['source.tar.gz'] = _source_context()
    initialize = params.get('initialize', True)
    if type(initialize) is not bool:
        raise InstanceError('invalid_initialize_scope')
    if initialize:
        from pydantic import ValidationError

        from app.config import Config
        initial = protected.get('initialConfig') or {}
        bootstrap = {'models': initial.get('models') or {'providers': {}, 'primary': ''}, 'memory': {'provider': 'builtin'},
                     'web': {'enabled': True, 'host': '0.0.0.0', 'port': 18961}}
        try:
            Config.model_validate(bootstrap)
        except (ValidationError, ValueError, TypeError):
            raise InstanceError('invalid_initial_config', 409) from None
        files['bootstrap.json'] = json.dumps(bootstrap, ensure_ascii=False).encode()
    directory = deploy_directory(item)
    command = f'umask 077; mkdir -p {shlex.quote(directory)} && tar -xf - -C {shlex.quote(directory)}'
    if 'source.tar.gz' in files:
        command += f' && rm -rf {shlex.quote(directory + "/source")} && mkdir -p {shlex.quote(directory + "/source")} && tar -xzf {shlex.quote(directory + "/source.tar.gz")} -C {shlex.quote(directory + "/source")}'
    if initialize:
        target = shlex.quote(item['workspacePath'] + '/openbear.json')
        command += (f' && mkdir -p {shlex.quote(item["workspacePath"])} && '
                    f'if [ ! -e {target} ]; then (set -C; cat {shlex.quote(directory + "/bootstrap.json")} > {target}); fi')
    return command, _tar(files), directory, image


async def perform(service, ctx, action, params):
    from app.tools.servers import _untrusted
    op = await service.authority(ctx, expected_revision=params.get('revision'))
    async with service.lock(op['instance_id']):
        op = await service.authority(ctx, expected_revision=params.get('revision'))
        row = await service.row(op['owner'], op['instance_id'])
        item = service.public(row)
        target = await service.servers.target(op['owner'], item['serverId'], nodes=True)
        name = '本机' if target.get('local') else target.get('name') or item.get('serverName') or target['id']

        async def run(command, **options):
            """The target's output and error text, labelled as data from it."""
            try:
                result = await service.servers.run(target, command, **options)
            except InstanceError as exc:
                exc.untrusted = name
                raise
            return _untrusted({**result, 'channel': target['channel']}, name)
        if action == 'deliver':
            protected = service.vault.open(row['protected'])
            await service.check_placement(op['owner'], item)
            command, content, directory, image = deployment_archive(row, protected, item, params)
            result = await run(command, stdin=content, timeout=300)
            if result['ok'] and mesh_bind(item):
                unit, install, text = boot_unit(item)
                if not (await run(install, stdin=text, timeout=60, root=True))['ok']:
                    result = {**result, 'ok': False, 'error': 'instance_boot_unit_failed'}
            if result['ok']:
                # The stored record, not its page view: notice windows and health state stay.
                latest = await service.row(op['owner'], op['instance_id'])
                record = json.loads(latest['public_json'])
                record['image'] = image
                async with service.db.write_transaction(label='instance-deployment-archive') as conn:
                    await conn.execute('UPDATE managed_instances SET public_json=? WHERE id=?', (json.dumps(record), row['id']))
            built = '' if not params.get('source', not item.get('image')) else f'；同版本源码在 {directory}/source，可用 docker build -t {image} {directory}/source 构建'
            return {**result, 'directory': directory, 'compose': directory + '/compose.json', 'image': image,
                    'note': f'已交付 compose.json（含实例凭证，不要打印）{built}。数据目录仅在缺少 openbear.json 时写入初始配置。'
                            f'然后执行 docker compose -f {directory}/compose.json up -d，再用 verify 核验。不删除已有数据目录。'}
        command = params.get('command')
        timeout = params.get('timeoutSeconds', 120)
        if not isinstance(command, str) or not command.strip() or len(command) > 32000 or type(timeout) is not int or not 1 <= timeout <= 600:
            raise InstanceError('invalid_remote_command')
        sudo = params.get('sudo', False)
        if type(sudo) is not bool:
            raise InstanceError('invalid_sudo_scope')
        if not isinstance(params.get('description', ''), str):
            raise InstanceError('invalid_remote_command')
        return await run(command, timeout=timeout, sudo=sudo)
