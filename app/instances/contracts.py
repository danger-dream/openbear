"""Strict controller-owned wire validation and closed child relay policy."""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InstanceError(ValueError):
    def __init__(self, error, status=400):
        super().__init__(error)
        self.error, self.status = error, status


def origin(value: str) -> str:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('invalid_origin')
    p = urlsplit(value)
    if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in {'', '/'}:
        raise ValueError('origin_required')
    host = p.hostname
    if '%' in host:
        raise ValueError('invalid_host')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?', host) or '..' in host:
            raise ValueError('invalid_host') from None
    else:
        if ip.is_unspecified or ip.is_multicast or ip.is_link_local:
            raise ValueError('invalid_host')
    if p.port is not None and not 1 <= p.port <= 65535:
        raise ValueError('invalid_port')
    rendered = f'[{host.lower()}]' if ':' in host else host.lower()
    if p.port and p.port != (443 if p.scheme == 'https' else 80):
        rendered += ':' + str(p.port)
    return p.scheme + '://' + rendered


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Account(Strict):
    username: str = Field(min_length=1, max_length=128)
    # Any non-empty password; the UI only advises on strength.
    password: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator('username')
    @classmethod
    def printable_username(cls, value):
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('printable_username_required')
        return value


class Notifications(Strict):
    loginSuccess: bool = False
    loginFailure: bool = True
    health: bool = True


class Models(Strict):
    # 'copy' copies the listed controller providers (default: only the primary model's);
    # 'later' leaves the instance empty.
    mode: str = Field(default='copy', pattern=r'^(copy|later)$')
    primary: str = Field(default='', max_length=300)
    providers: list[str] | None = None


class Form(Strict):
    name: str = Field(min_length=1, max_length=120)
    serverId: str = Field(min_length=1, max_length=64)
    # Optional overrides; empty means automatic (port picked, address = server + port).
    port: int = Field(default=0, ge=0, le=65535)
    managementUrl: str = ''
    image: str = Field(default='', max_length=512, pattern=r'^[a-zA-Z0-9._:/@-]*$')
    workspacePath: str = Field(default='', max_length=1024)
    # Container memory cap in MiB; 0 is no limit.
    memoryLimitMb: int = Field(default=0, ge=0, le=1048576)
    account: Account
    models: Models = Field(default_factory=Models)
    notifications: Notifications = Field(default_factory=Notifications)

    @field_validator('managementUrl')
    @classmethod
    def url(cls, v):
        return origin(v) if v else ''

    @field_validator('workspacePath')
    @classmethod
    def workspace(cls, v):
        if not v:
            return v
        if not v.startswith('/') or any(x in v for x in ('\n', '\r', '\x00', ':', '$')) or '..' in PurePosixPath(v).parts:
            raise ValueError('absolute_volume_path_required')
        if str(PurePosixPath(v)) in {'/', '/var', '/etc', '/usr', '/home', '/var/run', '/run', '/proc', '/sys', '/dev'}:
            raise ValueError('dedicated_volume_required')
        return str(PurePosixPath(v))

    @field_validator('memoryLimitMb')
    @classmethod
    def memory(cls, v):
        if v and v < 256:
            raise ValueError('memory_limit_too_small')
        return v

    @model_validator(mode='after')
    def automatic_defaults(self):
        slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', self.account.username).strip('-').lower()[:40] or 'instance'
        if not self.workspacePath:
            self.workspacePath = '/opt/openbear/instances/' + slug
        return self


# The data directory is mounted read-write into a container running as root.
# Neither a system tree nor anything inside one:
SYSTEM_TREES = ('/etc', '/usr', '/bin', '/sbin', '/lib', '/lib32', '/lib64', '/boot', '/proc', '/sys', '/dev', '/run',
                '/var/run', '/root', '/snap', '/var/lib/docker', '/var/lib/containerd',
                # the gateway's state (every instance's keys) on this machine, and the node's mesh keys
                '/var/lib/openbear-gateway', '/var/lib/openbear-node')
# nor a directory that holds other people's or other instances' data:
SHARED_DIRECTORIES = ('/', '/var', '/var/lib', '/home', '/opt', '/srv', '/mnt', '/media', '/tmp', '/var/tmp',
                      '/opt/openbear', '/opt/openbear/instances')


def check_workspace(path, protected=()):
    """Checked when an instance gets a data directory (created, or the path changed), so a saved
    instance keeps saving. protected: the controller's own directories on the same machine;
    the data directory may be neither one of them nor contain one."""
    p = PurePosixPath(path)
    if (str(p) in SHARED_DIRECTORIES or p.parent == PurePosixPath('/home')
            or any(p.is_relative_to(tree) for tree in SYSTEM_TREES)):
        raise InstanceError('dedicated_volume_required')
    if any(p == PurePosixPath(d) or PurePosixPath(d).is_relative_to(p) for d in protected):
        raise InstanceError('controller_directory_volume_forbidden')


EVENT_TYPES = {'login_success', 'login_failure', 'health', 'recovery'}

# Match decoded *segments*, as aiohttp's route placeholders do. Only the model
# ID placeholder permits an encoded slash; it is never a new route segment.
_ROUTE_RULES = [
    ('/api/managed/v1/status', {'GET'}),
    ('/api/managed/v1/config', {'GET', 'PATCH'}),
    ('/api/managed/v1/jobs', {'POST'}),
    ('/api/managed/v1/jobs/{name}', {'GET'}),
    ('/api/managed/v1/jobs/{name}/cancel', {'POST'}),
    *[(f'/api/managed/v1/files/{kind}/{{files}}', {'PUT'}) for kind in ('skills', 'mcp')],
    *[(f'/api/memory/{kind}', {'GET'}) for kind in ('categories', 'entries', 'secrets', 'docs', 'templates')],
    *[(f'/api/memory/{kind}', {'POST'}) for kind in ('entries', 'secrets', 'docs', 'templates', 'reorder', 'preview')],
    *[(f'/api/memory/{kind}/{{name}}', {'GET', 'PUT', 'DELETE'}) for kind in ('entries', 'secrets', 'docs')],
    ('/api/memory/templates/{name}', {'PUT', 'DELETE'}),
    ('/api/memory/templates/import-builtin', {'POST'}),
    ('/api/channels', {'GET', 'POST'}),
    *[(f'/api/channels/{action}', {'POST'}) for action in ('reorder', 'primary', 'compression', 'models/fetch')],
    ('/api/channels/{name}', {'GET', 'PATCH', 'DELETE'}),
    ('/api/channels/{name}/test', {'POST'}),
    ('/api/channels/{name}/test/{name}', {'GET'}),
    ('/api/channels/{name}/models', {'POST'}),
    ('/api/channels/{name}/models/reorder', {'POST'}),
    ('/api/channels/{name}/models-dev/matches', {'GET'}),
    *[(f'/api/channels/{{name}}/models-dev/{action}', {'POST'}) for action in ('preview', 'sync')],
    ('/api/channels/{name}/models/{model}', {'PATCH', 'DELETE'}),
    *[(f'/api/channels/{{name}}/models/{{model}}/{action}', {'POST'}) for action in ('test', 'models-dev/preview', 'models-dev/sync')],
    ('/api/mcp/status', {'GET'}),
    ('/api/mcp/servers/{name}/tools', {'GET'}),
    ('/api/mcp/enabled', {'PATCH'}),
    ('/api/mcp/reload', {'POST'}),
    *[(f'/api/mcp/servers/{{name}}/{action}', {'PATCH'}) for action in ('enabled', 'approval', 'agent-access', 'tools')],
    *[(f'/api/mcp/servers/{{name}}/{action}', {'POST'}) for action in ('refresh-tools', 'uninstall', 'confirm-exposure')],
    ('/api/skills', {'GET'}),
    ('/api/skills/reload', {'POST'}),
    ('/api/skills/{name}', {'GET'}),
    ('/api/skills/{name}/enabled', {'PATCH'}),
    ('/api/skills/{name}/uninstall', {'POST'}),
    ('/api/settings', {'GET'}),
    ('/api/settings/specs', {'GET'}),
    ('/api/settings/{setting}', {'PATCH'}),
]


def _route_matches(template, parts):
    route = template[1:].split('/')
    if route[-1] == '{files}':
        return (len(parts) >= len(route) and parts[:len(route)-1] == route[:-1]
                and all('/' not in part for part in parts[len(route)-1:]))
    if len(route) != len(parts):
        return False
    for expected, part in zip(route, parts, strict=True):
        if expected == '{model}':
            continue
        if expected == '{name}':
            if '/' in part:
                return False
            continue
        if expected == '{setting}':
            if not re.fullmatch(r'(agent|tools|memory|models|mcp|contextManagement)(?:\.[^/.]+)*', part):
                return False
            continue
        if expected != part:
            return False
    return True


def relay_path(method, path):
    """Return a canonical URL path; validate exactly once before URL libraries run."""
    if method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'} or not isinstance(path, str) or len(path) > 2048 or not path.startswith('/'):
        raise InstanceError('relay_route_forbidden', 403)
    if re.search(r'%(?![0-9a-fA-F]{2})', path):
        raise InstanceError('relay_route_forbidden', 403)
    try:
        parts = [unquote(part, encoding='utf-8', errors='strict') for part in path[1:].split('/')]
    except UnicodeError:
        raise InstanceError('relay_route_forbidden', 403) from None
    # Reject residual '%' (including double encoding), controls, URL suffixes,
    # backslashes and traversal even inside an opaque model ID. No Unicode
    # normalization: valid names must reach the child unchanged.
    if any(any(c in '%?#\\' or unicodedata.category(c).startswith('C') for c in part)
           or any(piece in {'', '.', '..'} for piece in part.split('/')) for part in parts):
        raise InstanceError('relay_route_forbidden', 403)
    if not any(method in methods and _route_matches(template, parts) for template, methods in _ROUTE_RULES):
        raise InstanceError('relay_route_forbidden', 403)
    return '/' + '/'.join(quote(part, safe='-._~:@') for part in parts)
