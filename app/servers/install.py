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
# openbear-node installer, for systemd Linux, macOS and OpenWrt (procd; Kwrt, ImmortalWrt...).
# Usage: sh install.sh --url URL --code CODE [--fingerprint SHA256] [--backup URL] [--node ID] [--force] [--no-exec] [--no-terminal] [--browser] [--stage STAGE]
#        [--pin SHA256] [--backup-pin SHA256] [--front] [--binary PATH]
# STAGE is all (default) or one of check, download, enroll, service; the
# controller runs them one by one over SSH to show progress.
# A machine already running openbear-node as another node (its config names
# a node id other than --node) is left alone unless --force is given.
# With --fingerprint the node enrolls only with a controller of that identity key.
# A front (--front) reaches the controller only through its exits, whose certificates --pin and
# --backup-pin name. --binary is the node program the controller sent here; without it the program
# is fetched through the exit, whose self-signed certificate is not checked: its checksum is.
set -eu
URL=""; BACKUP=""; CODE=""; FINGERPRINT=""; FLAGS=""; STAGE="all"; NODE=""; FORCE=""; BINARY=""; ACCESS=""; FRONT=""
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
    --pin|--backup-pin) ACCESS="$ACCESS $1 $2"; shift 2 ;;
    --front) ACCESS="$ACCESS --front"; FRONT=1; shift ;;
    --binary) BINARY="$2"; shift 2 ;;
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
# OpenWrt: procd starts an init script; a firmware upgrade keeps only what keep.d lists.
OPENWRT=""
[ "$OS" = linux ] && [ -f /etc/openwrt_release ] && OPENWRT=1
INIT=/etc/init.d/openbear-node
KEEP=/lib/upgrade/keep.d/openbear-node
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
# The node's own service: stop before replacing the program, start again after a refused enroll.
service_do() {
  if [ "$OS" = darwin ]; then
    if [ "$1" = stop ]; then launchctl bootout "system/$LABEL" 2>/dev/null || true; else [ -f "$PLIST" ] && launchctl bootstrap system "$PLIST" 2>/dev/null || true; fi
  elif [ -n "$OPENWRT" ]; then
    [ -x "$INIT" ] && "$INIT" "$1" 2>/dev/null || true
  else
    systemctl "$1" openbear-node 2>/dev/null || true
  fi
}
if [ -n "$OPENWRT" ]; then
  case "$FLAGS" in *--browser*) echo "no browser on OpenWrt: its musl system cannot run Chrome"; FLAGS=$(echo "$FLAGS" | sed 's/ --browser//g') ;; esac
fi
# Whether a front's exit answers on its front port; without curl or wget it is taken to.
reach() {
  if command -v curl >/dev/null 2>&1; then
    curl -sk -o /dev/null --connect-timeout 8 --max-time 20 "$1/api/server-nodes/v1/install.sh"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --no-check-certificate -T 8 -O /dev/null "$1/api/server-nodes/v1/install.sh"
  fi
}
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
  [ -n "$BINARY" ] || command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || { echo "curl or wget is required" >&2; exit 1; }
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || { echo "sha256sum is required" >&2; exit 1; }
  if [ "$OS" = darwin ]; then
    echo "arch=$ARCH macos=$(sw_vers -productVersion)"
  elif [ -n "$OPENWRT" ]; then
    echo "arch=$ARCH openwrt=$(. /etc/openwrt_release; echo "$DISTRIB_ID $DISTRIB_RELEASE" | tr ' ' '_')"
    # The mesh needs these; the rest of the node runs without them.
    MISSING=""
    [ -c /dev/net/tun ] || grep -qw tun /proc/modules 2>/dev/null || MISSING="$MISSING kmod-tun"
    ip -V 2>&1 | grep -q iproute2 || MISSING="$MISSING ip-full"
    command -v nft >/dev/null 2>&1 || MISSING="$MISSING nftables"
    command -v iptables >/dev/null 2>&1 || MISSING="$MISSING iptables-nft"
    [ -z "$MISSING" ] || echo "the mesh needs:$MISSING (opkg update && opkg install$MISSING)"
  else
    echo "arch=$ARCH systemd=$([ -d /run/systemd/system ] && echo yes || echo no)"
  fi
  if [ -n "$FRONT" ] && ! reach "$URL" && { [ -z "$BACKUP" ] || ! reach "$BACKUP"; }; then
    echo "@@unreachable $URL"
    echo "cannot reach the exit's front access $URL${BACKUP:+ or $BACKUP} from this machine." >&2
    echo "Allow this machine in on the exit's firewall for that TCP port; over a dedicated line it may come from a private address, not its public IP." >&2
    exit 1
  fi
fi
fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL ${FRONT:+-k} --connect-timeout 15 "$1/api/server-nodes/v1/download/openbear-node-$OS-$ARCH" -o "$2"
  else
    wget -q ${FRONT:+--no-check-certificate} -T 15 -O "$2" "$1/api/server-nodes/v1/download/openbear-node-$OS-$ARCH"
  fi
}
if stage download; then
  step download
  TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
  if [ -n "$BINARY" ]; then
    mv "$BINARY" "$TMP/openbear-node" || { echo "the node program sent by the controller is missing" >&2; exit 1; }
  else
    fetch "$URL" "$TMP/openbear-node" || { [ -n "$BACKUP" ] && echo "primary address failed, trying backup" && fetch "$BACKUP" "$TMP/openbear-node"; } \
      || { echo "cannot download the node from the controller" >&2; exit 1; }
  fi
  [ "$(sha256 "$TMP/openbear-node")" = "$SUM" ] || { echo "download checksum mismatch" >&2; exit 1; }
  mkdir -p /usr/local/bin
  # Written in full beside the old program while that still runs: a full disk leaves the node running.
  # OpenWrt has no install(1).
  cp "$TMP/openbear-node" /usr/local/bin/.openbear-node.new && chmod 0755 /usr/local/bin/.openbear-node.new \
    || { rm -f /usr/local/bin/.openbear-node.new; echo "cannot write /usr/local/bin (disk full?)" >&2; exit 1; }
  service_do stop
  if ! mv -f /usr/local/bin/.openbear-node.new /usr/local/bin/openbear-node; then
    rm -f /usr/local/bin/.openbear-node.new
    [ -f "$CONF" ] && service_do start
    echo "cannot replace /usr/local/bin/openbear-node; the node that was installed here is started again" >&2
    exit 1
  fi
fi
if stage enroll; then
  step enroll
  [ -n "$CODE" ] || { echo "--code is required" >&2; exit 2; }
  # In the environment, which a node build from before fingerprints ignores.
  if ! OPENBEAR_CONTROLLER_FINGERPRINT="$FINGERPRINT" /usr/local/bin/openbear-node enroll --url "$URL" ${BACKUP:+--backup "$BACKUP"} --code "$CODE" $FLAGS $ACCESS; then
    # Refused (an expired code, a typo): a node that ran here keeps its config and starts again.
    if [ -f "$CONF" ]; then
      service_do start
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
  elif [ -n "$OPENWRT" ]; then
    # Like the systemd unit: restarted 5 s after it exits, without limit; stopping it ends the
    # node alone, not what was started from a web terminal. Before the mesh (START=99).
    cat > "$INIT" <<'INIT'
#!/bin/sh /etc/rc.common
# OpenBear node
START=95
STOP=10
USE_PROCD=1

start_service() {
	procd_open_instance
	procd_set_param command /usr/local/bin/openbear-node run --config /etc/openbear-node/config.json
	procd_set_param env HOME=/root
	procd_set_param respawn 3600 5 0
	procd_set_param stdout 1
	procd_set_param stderr 1
	procd_close_instance
}
INIT
    chmod 0755 "$INIT"
    mkdir -p "$(dirname "$KEEP")"
    cat > "$KEEP" <<'KEEP'
/usr/local/bin/openbear-node
/etc/openbear-node/
/etc/init.d/openbear-node
/etc/rc.d/S95openbear-node
/etc/rc.d/K10openbear-node
/etc/init.d/openbear-mesh
/etc/rc.d/S99openbear-mesh
/etc/rc.d/K10openbear-mesh
/lib/upgrade/keep.d/openbear-node
KEEP
    "$INIT" enable
    "$INIT" stop >/dev/null 2>&1 || true
    "$INIT" start
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
    """Why the build in node/bin is passed over: 'invalid', 'protocol', 'features', 'older'
    (than the GitHub release the last check picked), or '' (used, or none there)."""
    from app.servers import releases
    value = local_manifest() if value is None else value
    if not value:
        return ''
    if not releases.valid_manifest(value, local=True):
        return 'invalid'
    if reason := releases.problem(value, local=True):
        return reason
    picked = releases.cached_manifest().get('version', '')
    return 'older' if picked and releases.newer(picked, str(value.get('version') or '')) else ''


def manifest():
    """The node build installs and updates use: a local one in node/bin (a development
    checkout) when it speaks this controller's protocol, has what it needs and is not older
    than the GitHub release the last check picked; else that release."""
    from app.servers import releases
    value = local_manifest()
    if value and not local_problem(value):
        return {**value, 'source': 'local'}
    return releases.cached_manifest()


WINDOWS_SCRIPT = r'''# openbear-node installer for Windows 10 / Server 2016 and newer.
# Run from an elevated PowerShell:
#   & ([scriptblock]::Create((irm URL/api/server-nodes/v1/install.ps1))) -Url URL -Code CODE [-Fingerprint SHA256] [-Backup URL] [-Node ID] [-Force] [-NoExec] [-NoTerminal] [-Browser] [-Stage STAGE]
#     [-Pin SHA256] [-BackupPin SHA256] [-Front] [-Binary PATH]
# Failures throw rather than exit, so a pasted command leaves the window open.
# A machine already running openbear-node as another node (its config names
# a node id other than -Node) is left alone unless -Force is given.
# With -Fingerprint the node enrolls only with a controller of that identity key.
# A front (-Front) reaches the controller only through its exits, whose certificates -Pin and
# -BackupPin name. -Binary is the node program the controller sent here, a path relative to the
# login's home as scp puts it; without it the program is fetched through the exit, whose
# self-signed certificate is not checked: its checksum is.
param([string]$Url = '', [string]$Code = '', [string]$Fingerprint = '', [string]$Backup = '', [string]$Stage = 'all', [string]$Node = '', [switch]$Force, [switch]$NoExec, [switch]$NoTerminal, [switch]$Browser,
  [string]$Pin = '', [string]$BackupPin = '', [switch]$Front, [string]$Binary = '')
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
  if ($Front) {
    # Whether a front's exit answers on its front port.
    $reached = $false
    foreach ($base in @($Url, $Backup) | Where-Object { $_ }) {
      $client = New-Object Net.Sockets.TcpClient
      try { $reached = $client.ConnectAsync(([Uri]$base).DnsSafeHost, ([Uri]$base).Port).Wait(8000) } catch { $reached = $false } finally { $client.Dispose() }
      if ($reached) { break }
    }
    if (-not $reached) {
      "@@unreachable $Url"
      throw "cannot reach the exit's front access $Url from this machine. Allow this machine in on the exit's firewall for that TCP port; over a dedicated line it may come from a private address, not its public IP."
    }
  }
}
function Get-Node($base, $to) {
  $uri = "$base/api/server-nodes/v1/download/openbear-node-windows-$arch.exe"
  if (-not $Front) { Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $uri -OutFile $to; return }
  if ($PSVersionTable.PSVersion.Major -ge 6) { Invoke-WebRequest -UseBasicParsing -SkipCertificateCheck -TimeoutSec 120 -Uri $uri -OutFile $to; return }
  [Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
  try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $uri -OutFile $to } finally { [Net.ServicePointManager]::ServerCertificateValidationCallback = $null }
}
if (Test-Stage 'download') {
  '@@step download'
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ('openbear-node-' + [guid]::NewGuid().ToString('N') + '.exe')
  if ($Binary) {
    if (-not [IO.Path]::IsPathRooted($Binary)) { $Binary = Join-Path $HOME $Binary }
    if (-not (Test-Path -LiteralPath $Binary)) { throw 'the node program sent by the controller is missing' }
    Move-Item -Force -LiteralPath $Binary $tmp
  } else {
    try {
      try { Get-Node $Url $tmp } catch {
        if (-not $Backup) { throw }
        'primary address failed, trying backup'
        Get-Node $Backup $tmp
      }
    } catch { throw 'cannot download the node from the controller' }
  }
  if ((Get-FileHash -Algorithm SHA256 $tmp).Hash.ToLower() -ne $sum) { Remove-Item -Force $tmp; throw 'download checksum mismatch' }
  $service = Get-Service $name -ErrorAction SilentlyContinue
  if ($service -and $service.Status -ne 'Stopped') { Stop-Service $name -Force; $service.WaitForStatus('Stopped', '00:00:30') }
  New-Item -ItemType Directory -Force $dir | Out-Null
  Move-Item -Force $tmp $exe
}
if (Test-Stage 'enroll') {
  '@@step enroll'
  if (-not $Code) { throw '-Code is required' }
  $arguments = @('enroll', '--url', $Url, '--code', $Code, '--config', $config)
  if ($Backup) { $arguments += @('--backup', $Backup) }
  if ($Pin) { $arguments += @('--pin', $Pin) }
  if ($Backup -and $BackupPin) { $arguments += @('--backup-pin', $BackupPin) }
  if ($Front) { $arguments += '--front' }
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


def arguments(url, code, backup='', stage='all', *, browser=False, node='', force=False, fingerprint='', pin='', backup_pin='', front=False, binary=''):
    parts = ['--url', url.rstrip('/'), '--code', code]
    if fingerprint:
        parts += ['--fingerprint', fingerprint]
    if backup:
        parts += ['--backup', backup.rstrip('/')]
    if pin:
        parts += ['--pin', pin]
    if backup and backup_pin:
        parts += ['--backup-pin', backup_pin]
    if front:
        parts += ['--front']
    if binary:
        parts += ['--binary', binary]
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


def windows_arguments(url, code, backup='', stage='all', *, browser=False, node='', force=False, fingerprint='', pin='', backup_pin='', front=False, binary=''):
    parts = f'-Url {ps(url.rstrip("/"))} -Code {ps(code)}'
    if fingerprint:
        parts += f' -Fingerprint {ps(fingerprint)}'
    if backup:
        parts += f' -Backup {ps(backup.rstrip("/"))}'
    if pin:
        parts += f' -Pin {ps(pin)}'
    if backup and backup_pin:
        parts += f' -BackupPin {ps(backup_pin)}'
    if front:
        parts += ' -Front'
    if binary:
        parts += f' -Binary {ps(binary)}'
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


def front_command(url, code, *, backup='', browser=False, node='', fingerprint='', pin='', backup_pin=''):
    """A front's command: install.sh comes through its exit, whose self-signed certificate is not
    checked, so the script's checksum is before it runs."""
    base = url.rstrip('/')
    digest = hashlib.sha256(script().encode()).hexdigest()
    # A subshell keeps a pasted command's exit/trap out of the login shell. Cleanup
    # must preserve failures from mktemp, curl, checksum verification and the installer.
    return ('( f=$(mktemp) || exit $?; trap \'r=$?; rm -f "$f" || :; exit "$r"\' 0; '
            'curl -fsSk --connect-timeout 15 ' + shlex.quote(base + '/api/server-nodes/v1/install.sh') + ' -o "$f" || { r=$?; echo '
            + shlex.quote(f'cannot fetch install.sh through the exit at {base}; if it does not answer, allow this machine in on the exit\'s firewall '
                          'for that TCP port (over a dedicated line it may come from a private address)') + ' >&2; exit "$r"; }; '
            '[ "$( (sha256sum "$f" 2>/dev/null || shasum -a 256 "$f") | cut -d" " -f1)" = ' + digest + ' ] '
            '|| { echo "install.sh checksum mismatch" >&2; exit 1; }; sh "$f" '
            + arguments(base, code, backup, browser=browser, node=node, fingerprint=fingerprint, pin=pin, backup_pin=backup_pin, front=True) + ' )')


def windows_front_command(url, code, *, backup='', browser=False, node='', fingerprint='', pin='', backup_pin=''):
    """front_command for an elevated PowerShell, 5.1 or 7."""
    base = url.rstrip('/')
    digest = hashlib.sha256(windows_script().encode()).hexdigest()
    source = ps(base + '/api/server-nodes/v1/install.ps1')
    return ('[Net.ServicePointManager]::SecurityProtocol=3072; '
            f'if ($PSVersionTable.PSVersion.Major -ge 6) {{ $s = irm -SkipCertificateCheck {source} }} '
            'else { [Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }; '
            f'try {{ $s = irm {source} }} finally {{ [Net.ServicePointManager]::ServerCertificateValidationCallback = $null }} }}; '
            "if ((-join ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s)) | ForEach-Object { $_.ToString('x2') })) "
            f"-ne '{digest}') {{ throw 'install.ps1 checksum mismatch' }}; & ([scriptblock]::Create($s)) "
            + windows_arguments(base, code, backup, browser=browser, node=node, fingerprint=fingerprint, pin=pin, backup_pin=backup_pin, front=True))


def windows_ssh_command(url, code, *, script, backup='', stage='all', browser=False, node='', force=False, fingerprint='', pin='', backup_pin='', front=False, binary=''):
    """One line that works whether the SSH server's shell is cmd or PowerShell. It runs
    windows_script(), which scp put into the login's home as script, and removes it. cmd caps a
    command line at 8191 characters, a front could not fetch the script, and an older Windows
    OpenSSH (Windows 10 1809) at times never ends a command's stdin."""
    arguments = windows_arguments(url, code, backup, stage, browser=browser, node=node, force=force, fingerprint=fingerprint,
                                  pin=pin, backup_pin=backup_pin, front=front, binary=binary)
    body = ("$ProgressPreference='SilentlyContinue'\n"
            'try {\n'
            f'  $p = Join-Path $HOME {ps(script)}\n'
            '  $s = [IO.File]::ReadAllText($p); Remove-Item -Force -LiteralPath $p\n'
            f'  & ([scriptblock]::Create($s)) {arguments}\n'
            '} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n')
    encoded = base64.b64encode(body.encode('utf-16-le')).decode()
    return f'powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}'
