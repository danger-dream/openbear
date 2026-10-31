"""Node binaries shipped with this controller and the install scripts that
fetch them: install.sh for Linux and macOS, install.ps1 for Windows. None
contains a secret: the one-time code is an argument."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / 'node/bin'

SCRIPT = r'''#!/bin/sh
# openbear-node installer.
# Usage: sh install.sh --url URL --code CODE [--fingerprint SHA256] [--backup URL] [--node ID] [--force] [--no-exec] [--no-terminal] [--browser] [--stage STAGE]
# STAGE is all (default) or one of check, download, enroll, service; the
# controller runs them one by one over SSH to show progress.
# A machine already running openbear-node as another node (its config names
# a node id other than --node) is left alone unless --force is given.
# With --fingerprint the node enrolls only with a controller of that identity key.
set -eu
URL=""; BACKUP=""; CODE=""; FINGERPRINT=""; FLAGS=""; STAGE="all"; NODE=""; FORCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --backup) BACKUP="$2"; shift 2 ;;
    --code) CODE="$2"; shift 2 ;;
    --fingerprint) FINGERPRINT="$2"; shift 2 ;;
    --stage) STAGE="$2"; shift 2 ;;
    --node) NODE="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-exec|--no-terminal|--browser) FLAGS="$FLAGS $1"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
stage() { [ "$STAGE" = all ] || [ "$STAGE" = "$1" ]; }
step() { echo "@@step $1"; }
[ -n "$URL" ] || { echo "--url is required" >&2; exit 2; }
case "$(uname -s)" in
  Linux) OS=linux ;;
  Darwin) OS=darwin ;;
  *) echo "unsupported system: $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
case "$OS-$ARCH" in
  linux-amd64) SUM="@SUM_linux_amd64@" ;;
  linux-arm64) SUM="@SUM_linux_arm64@" ;;
  darwin-amd64) SUM="@SUM_darwin_amd64@" ;;
  darwin-arm64) SUM="@SUM_darwin_arm64@" ;;
esac
LABEL=com.openbear.node
PLIST=/Library/LaunchDaemons/$LABEL.plist
CONF=/etc/openbear-node/config.json
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
if stage check; then
  step check
  [ "$(id -u)" = 0 ] || { echo "openbear-node must be installed as root (use sudo)" >&2; exit 1; }
  # Installing writes the same config and service: the node already here would be replaced.
  OLD=""
  [ -f "$CONF" ] && OLD=$(sed -n 's/.*"nodeId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONF" | head -n 1)
  if [ -n "$OLD" ]; then
    echo "@@node $OLD"
    if [ "$OLD" != "$NODE" ] && [ -z "$FORCE" ]; then
      echo "this machine already runs openbear-node as node $OLD ($CONF); installing would replace it." >&2
      echo "Remove that server on its controller or reinstall from there, or run again with --force to replace it." >&2
      exit 3
    fi
  fi
  [ -n "$SUM" ] || { echo "the controller has no openbear-node build for $OS-$ARCH" >&2; exit 1; }
  command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || { echo "curl or wget is required" >&2; exit 1; }
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || { echo "sha256sum is required" >&2; exit 1; }
  if [ "$OS" = darwin ]; then
    echo "arch=$ARCH macos=$(sw_vers -productVersion)"
  else
    echo "arch=$ARCH systemd=$([ -d /run/systemd/system ] && echo yes || echo no)"
  fi
fi
fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 15 "$1/api/server-nodes/v1/download/openbear-node-$OS-$ARCH" -o "$2"
  else
    wget -q -T 15 -O "$2" "$1/api/server-nodes/v1/download/openbear-node-$OS-$ARCH"
  fi
}
if stage download; then
  step download
  TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
  fetch "$URL" "$TMP/openbear-node" || { [ -n "$BACKUP" ] && echo "primary address failed, trying backup" && fetch "$BACKUP" "$TMP/openbear-node"; } \
    || { echo "cannot download the node from the controller" >&2; exit 1; }
  [ "$(sha256 "$TMP/openbear-node")" = "$SUM" ] || { echo "download checksum mismatch" >&2; exit 1; }
  if [ "$OS" = darwin ]; then
    launchctl bootout "system/$LABEL" 2>/dev/null || true
    mkdir -p /usr/local/bin
  else
    systemctl stop openbear-node 2>/dev/null || true
  fi
  install -m 0755 "$TMP/openbear-node" /usr/local/bin/openbear-node
fi
if stage enroll; then
  step enroll
  [ -n "$CODE" ] || { echo "--code is required" >&2; exit 2; }
  # In the environment, which a node build from before fingerprints ignores.
  if ! OPENBEAR_CONTROLLER_FINGERPRINT="$FINGERPRINT" /usr/local/bin/openbear-node enroll --url "$URL" ${BACKUP:+--backup "$BACKUP"} --code "$CODE" $FLAGS; then
    # Refused (an expired code, a typo): a node that ran here keeps its config and starts again.
    if [ -f "$CONF" ]; then
      if [ "$OS" = darwin ]; then
        [ -f "$PLIST" ] && launchctl bootstrap system "$PLIST" 2>/dev/null || true
      else
        systemctl start openbear-node 2>/dev/null || true
      fi
      echo "enroll failed; the node that was installed here is started again" >&2
    fi
    exit 1
  fi
fi
if stage service; then
  step service
  if [ "$OS" = darwin ]; then
    # launchd gives daemons a bare PATH; Homebrew and /usr/local tools should work in commands and terminals.
    cat > "$PLIST" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.openbear.node</string>
  <key>ProgramArguments</key>
  <array><string>/usr/local/bin/openbear-node</string><string>run</string><string>--config</string><string>/etc/openbear-node/config.json</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardErrorPath</key><string>/var/log/openbear-node.log</string>
</dict>
</plist>
PLIST
    chmod 0644 "$PLIST"
    launchctl bootout "system/$LABEL" 2>/dev/null || true
    launchctl bootstrap system "$PLIST"
    launchctl enable "system/$LABEL"
    echo "openbear-node installed and started"
  elif command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    cat > /etc/systemd/system/openbear-node.service <<'UNIT'
[Unit]
Description=OpenBear node
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
ExecStart=/usr/local/bin/openbear-node run --config /etc/openbear-node/config.json
Restart=always
RestartSec=5
# Like sshd: restarting or updating the node must not kill what was started
# from a web terminal (nohup, tmux); open shells still end when it exits.
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable openbear-node >/dev/null 2>&1
    systemctl restart openbear-node
    echo "openbear-node installed and started"
  else
    echo "openbear-node installed; no systemd found, start it with:"
    echo "  /usr/local/bin/openbear-node run --config /etc/openbear-node/config.json"
  fi
fi
'''


def local_manifest():
    """node/bin's manifest as built, whether or not this controller can use it; {} without one."""
    try:
        value = json.loads((BIN / 'manifest.json').read_text())
    except (OSError, ValueError):
        value = {}
    return value if isinstance(value, dict) and value.get('binaries') else {}


def local_problem(value=None):
    """Why the build in node/bin is passed over: 'invalid', 'protocol', 'features', or '' (used, or none there)."""
    from app.servers import releases
    value = local_manifest() if value is None else value
    if not value:
        return ''
    if not releases.valid_manifest(value, local=True):
        return 'invalid'
    return releases.problem(value, local=True)


def manifest():
    """The node build installs and updates use: a local one in node/bin (a development
    checkout) when it speaks this controller's protocol and has what it needs, else the
    GitHub release the last check picked."""
    from app.servers import releases
    value = local_manifest()
    if value and not local_problem(value):
        return {**value, 'source': 'local'}
    return releases.cached_manifest()


WINDOWS_SCRIPT = r'''# openbear-node installer for Windows 10 / Server 2016 and newer.
# Run from an elevated PowerShell:
#   & ([scriptblock]::Create((irm URL/api/server-nodes/v1/install.ps1))) -Url URL -Code CODE [-Fingerprint SHA256] [-Backup URL] [-Node ID] [-Force] [-NoExec] [-NoTerminal] [-Browser] [-Stage STAGE]
# Failures throw rather than exit, so a pasted command leaves the window open.
# A machine already running openbear-node as another node (its config names
# a node id other than -Node) is left alone unless -Force is given.
# With -Fingerprint the node enrolls only with a controller of that identity key.
param([string]$Url = '', [string]$Code = '', [string]$Fingerprint = '', [string]$Backup = '', [string]$Stage = 'all', [string]$Node = '', [switch]$Force, [switch]$NoExec, [switch]$NoTerminal, [switch]$Browser)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
function Test-Stage($name) { $Stage -eq 'all' -or $Stage -eq $name }
if (-not $Url) { throw '-Url is required' }
$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$arch = @{ AMD64 = 'amd64'; ARM64 = 'arm64' }[$arch]
$sum = @{ amd64 = '@SUM_windows_amd64@'; arm64 = '@SUM_windows_arm64@' }[$arch]
$dir = Join-Path $env:ProgramFiles 'OpenBear'
$exe = Join-Path $dir 'openbear-node.exe'
$config = Join-Path $env:ProgramData 'OpenBear\node\config.json'
$name = 'openbear-node'
if (Test-Stage 'check') {
  '@@step check'
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'openbear-node must be installed from an elevated (Administrator) PowerShell' }
  # Installing writes the same config and service: the node already here would be replaced.
  $old = ''
  if (Test-Path -LiteralPath $config) { try { $old = [string](Get-Content -Raw -LiteralPath $config | ConvertFrom-Json).nodeId } catch { $old = '' } }
  if ($old) {
    "@@node $old"
    if ($old -ne $Node -and -not $Force) {
      throw "this machine already runs openbear-node as node $old ($config); installing would replace it. Remove that server on its controller or reinstall from there, or run again with -Force to replace it."
    }
  }
  $build = [Environment]::OSVersion.Version.Build
  if ($build -lt 14393) { throw 'openbear-node needs Windows 10 / Server 2016 or newer' }
  if (-not $arch) { throw "unsupported architecture: $env:PROCESSOR_ARCHITECTURE" }
  if (-not $sum) { throw "the controller has no openbear-node build for windows-$arch" }
  "arch=$arch build=$build"
  if ($build -lt 17763) { 'note: the web terminal needs Windows 10 1809 / Server 2019 or newer' }
}
function Get-Node($base, $to) {
  Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri "$base/api/server-nodes/v1/download/openbear-node-windows-$arch.exe" -OutFile $to
}
if (Test-Stage 'download') {
  '@@step download'
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ('openbear-node-' + [guid]::NewGuid().ToString('N') + '.exe')
  try {
    try { Get-Node $Url $tmp } catch {
      if (-not $Backup) { throw }
      'primary address failed, trying backup'
      Get-Node $Backup $tmp
    }
  } catch { throw 'cannot download the node from the controller' }
  if ((Get-FileHash -Algorithm SHA256 $tmp).Hash.ToLower() -ne $sum) { Remove-Item -Force $tmp; throw 'download checksum mismatch' }
  $service = Get-Service $name -ErrorAction SilentlyContinue
  if ($service -and $service.Status -ne 'Stopped') { Stop-Service $name -Force; $service.WaitForStatus('Stopped', '00:00:30') }
  New-Item -ItemType Directory -Force $dir | Out-Null
  Move-Item -Force $tmp $exe
}
if (Test-Stage 'enroll') {
  '@@step enroll'
  if (-not $Code) { throw '-Code is required' }
  $arguments = @('enroll', '--url', $Url, '--code', $Code)
  if ($Backup) { $arguments += @('--backup', $Backup) }
  if ($NoExec) { $arguments += '--no-exec' }
  if ($NoTerminal) { $arguments += '--no-terminal' }
  if ($Browser) { $arguments += '--browser' }
  # In the environment, which a node build from before fingerprints ignores.
  $env:OPENBEAR_CONTROLLER_FINGERPRINT = $Fingerprint
  try { & $exe @arguments } finally { Remove-Item Env:OPENBEAR_CONTROLLER_FINGERPRINT -ErrorAction SilentlyContinue }
  if ($LASTEXITCODE -ne 0) {
    $code = $LASTEXITCODE
    # Refused (an expired code, a typo): a node that ran here keeps its config and starts again.
    if ((Test-Path -LiteralPath $config) -and (Get-Service $name -ErrorAction SilentlyContinue)) {
      Start-Service $name -ErrorAction SilentlyContinue
      throw "enroll failed with exit code $code; the node that was installed here is started again"
    }
    throw "enroll failed with exit code $code"
  }
}
if (Test-Stage 'service') {
  '@@step service'
  $line = '"' + $exe + '" run --config "' + $config + '"'
  if (Get-Service $name -ErrorAction SilentlyContinue) {
    sc.exe config $name binPath= $line start= auto | Out-Null
  } else {
    New-Service -Name $name -BinaryPathName $line -DisplayName 'OpenBear Node' -Description 'Connects this machine to an OpenBear controller.' -StartupType Automatic | Out-Null
  }
  # Restart after any exit with an error, which is also how an update restarts.
  sc.exe failure $name reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null
  sc.exe failureflag $name 1 | Out-Null
  Restart-Service $name -Force
  'openbear-node installed and started'
}
'''

PLATFORMS = ('linux', 'darwin', 'windows')


def platform_of(info):
    value = (info or {}).get('platform')
    return value if value in PLATFORMS else 'linux'


def binary_name(platform, arch):
    return f'openbear-node-{platform}-{arch}' + ('.exe' if platform == 'windows' else '')


def _sums(text):
    binaries = manifest().get('binaries') or {}
    for platform in PLATFORMS:
        for arch in ('amd64', 'arm64'):
            text = text.replace(f'@SUM_{platform}_{arch}@', (binaries.get(binary_name(platform, arch)) or {}).get('sha256', ''))
    return text


def script():
    return _sums(SCRIPT)


def windows_script():
    return _sums(WINDOWS_SCRIPT)


def binary(name):
    """Path of a shipped binary, only for names the manifest lists."""
    value = manifest()
    if name not in (value.get('binaries') or {}):
        return None
    if value.get('source') == 'local':
        path = BIN / name
    else:
        from app.servers import releases
        path = releases.cache_dir(value['version']) / name
    return path if path.is_file() else None


async def fetch_binary(name):
    """binary(), downloading a release binary on its first use."""
    if path := binary(name):
        return path
    if manifest().get('source') != 'github':
        return None
    from app.servers import releases
    return await releases.fetch(name)


STAGES = ('check', 'download', 'enroll', 'service')
_NODE_LINE = re.compile(r'^@@node (\S{1,80})\s*$', re.M)


def existing_node(output):
    """The node id the check stage found in the machine's node config, '' for none."""
    found = _NODE_LINE.search(output or '')
    return found.group(1) if found else ''


def fingerprint(controller_key):
    """SHA-256 of this controller's identity key (base64), which a node checks when it enrolls."""
    return hashlib.sha256(base64.b64decode(controller_key)).hexdigest()


def arguments(url, code, backup='', stage='all', *, browser=False, node='', force=False, fingerprint=''):
    parts = ['--url', url.rstrip('/'), '--code', code]
    if fingerprint:
        parts += ['--fingerprint', fingerprint]
    if backup:
        parts += ['--backup', backup.rstrip('/')]
    if node:
        parts += ['--node', node]
    if force:
        parts += ['--force']
    if browser:
        parts += ['--browser']
    if stage != 'all':
        parts += ['--stage', stage]
    return ' '.join(shlex.quote(p) for p in parts)


def command(url, code, *, backup='', sudo=False, stage='all', browser=False, node='', force=False, fingerprint=''):
    base = url.rstrip('/')
    shell = 'sudo sh' if sudo else 'sh'
    return (f'curl -fsSL {shlex.quote(base + "/api/server-nodes/v1/install.sh")} | {shell} -s -- '
            + arguments(base, code, backup, stage, browser=browser, node=node, force=force, fingerprint=fingerprint))


def ps(value):
    """A PowerShell single-quoted literal. PowerShell also ends such a string
    at the typographic single quotes, so each of them is doubled as well."""
    return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r'\1\1', value) + "'"


def windows_arguments(url, code, backup='', stage='all', *, browser=False, node='', force=False, fingerprint=''):
    parts = f'-Url {ps(url.rstrip("/"))} -Code {ps(code)}'
    if fingerprint:
        parts += f' -Fingerprint {ps(fingerprint)}'
    if backup:
        parts += f' -Backup {ps(backup.rstrip("/"))}'
    if node:
        parts += f' -Node {ps(node)}'
    if force:
        parts += ' -Force'
    if browser:
        parts += ' -Browser'
    if stage != 'all':
        parts += f' -Stage {ps(stage)}'
    return parts


def windows_command(url, code, *, backup='', browser=False, node='', fingerprint=''):
    """What to paste into an elevated PowerShell."""
    base = url.rstrip('/')
    return (f'[Net.ServicePointManager]::SecurityProtocol=3072; & ([scriptblock]::Create((irm {ps(base + "/api/server-nodes/v1/install.ps1")}))) '
            + windows_arguments(base, code, backup, browser=browser, node=node, fingerprint=fingerprint))


def windows_ssh_command(url, code, *, backup='', stage='all', browser=False, node='', force=False, fingerprint=''):
    """One line that works whether the SSH server's shell is cmd or PowerShell.
    The script is fetched (from the backup address if needed) because cmd caps
    a command line at 8191 characters."""
    sources = ','.join(ps(u.rstrip('/') + '/api/server-nodes/v1/install.ps1') for u in (url, backup) if u)
    body = ("$ProgressPreference='SilentlyContinue'\n"
            '[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072\n'
            'try {\n'
            "  $s = $null; $why = ''\n"
            f'  foreach ($u in @({sources})) {{ try {{ $s = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri $u).Content; break }} catch {{ $why = $_.Exception.Message }} }}\n'
            "  if ($s -is [byte[]]) { $s = [Text.Encoding]::UTF8.GetString($s) }\n"
            '  if (-not $s) { throw "cannot reach the controller: $why" }\n'
            f'  & ([scriptblock]::Create($s)) {windows_arguments(url, code, backup, stage, browser=browser, node=node, force=force, fingerprint=fingerprint)}\n'
            '} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n')
    encoded = base64.b64encode(body.encode('utf-16-le')).decode()
    return f'powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}'
