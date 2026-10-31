"""Running a command over a server's saved SSH login while its node is offline.
The script goes to the remote shell on stdin, so nothing is quoted into the
command line; the shell's own stdin therefore cannot carry other input."""
from __future__ import annotations

import base64
import html
import re
import shlex

from app.servers.install import ps

# Printed first by the script, so the controller knows which shell ran it and
# that it started at all.
MARKER = '\x1eopenbear-shell='
# Through `sh -c` so a login shell like fish or csh never parses the test.
UNIX_COMMAND = "exec /bin/sh -c 'if [ -x /bin/bash ]; then exec /bin/bash -l -s; else exec /bin/sh -s; fi'"
PS_BOOTSTRAP = ("$ProgressPreference='SilentlyContinue'\n"
                "try { [Console]::OutputEncoding=[Text.Encoding]::UTF8 } catch {}\n"
                "$OutputEncoding=[Text.Encoding]::UTF8\n"
                "$ms=New-Object IO.MemoryStream\n[Console]::OpenStandardInput().CopyTo($ms)\n"
                ". ([scriptblock]::Create([Text.Encoding]::UTF8.GetString($ms.ToArray())))\n")
PS_EPILOGUE = '\nif ($?) { exit 0 } elseif ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }\n'
# ssh's own failures before the remote side ran anything.
_NOT_STARTED = re.compile(r'^ssh: connect to host |^ssh: Could not resolve hostname |Permission denied \(|Host key verification failed'
                          r'|kex_exchange_identification|Connection closed by \S+ port \d+|banner exchange|Too many authentication failures', re.M)
_CLIXML_STREAM = re.compile(r'<S S="(\w+)">([^<]*)</S>')
_CLIXML_ESCAPE = re.compile(r'_x([0-9A-Fa-f]{4})_')


def script(command, platform, cwd=''):
    """(remote command line, stdin bytes) for one command."""
    if platform == 'windows':
        body = (f'Set-Location -LiteralPath {ps(cwd)} -ErrorAction Stop\n' if cwd else '') + command + PS_EPILOGUE
        encoded = base64.b64encode(PS_BOOTSTRAP.encode('utf-16-le')).decode()
        line = ('powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -OutputFormat Text '
                f'-EncodedCommand {encoded}')
        return line, body.encode()
    # The braces make the shell read the whole command before running it, so
    # nothing in it can read the rest of the script as its input.
    body = (f'printf \'{MARKER}%s\\n\' "${{BASH_VERSION:+bash}}"\n'
            + (f'cd -- {shlex.quote(cwd)} || exit 1\n' if cwd else '')
            + '{\n' + command + '\n} </dev/null\n')
    return UNIX_COMMAND, body.encode()


def plain_errors(text):
    """Windows PowerShell's CLIXML stderr as the text a console shows."""
    head, found, body = text.partition('#< CLIXML')
    if not found:
        return text
    out = [head]
    for stream, raw in _CLIXML_STREAM.findall(body):
        value = _CLIXML_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), html.unescape(raw))
        out.append(value if stream == 'Error' else f'{stream.upper()}: {value}')
    return ''.join(out)


def result(raw, platform):
    """An SSH run as an exec result. A dropped connection or a timeout leaves
    the effect unknown; only ssh's own failures before the script started mean
    nothing ran."""
    stdout, stderr = raw.get('stdout') or '', raw.get('stderr') or ''
    shell, started = ('powershell' if platform == 'windows' else ''), False
    if platform != 'windows':
        at = stdout.find(MARKER)
        if at >= 0:
            end = stdout.find('\n', at)
            end = len(stdout) if end < 0 else end + 1
            shell, started = ('bash' if stdout[at + len(MARKER):end].strip() == 'bash' else 'sh'), True
            stdout = stdout[:at] + stdout[end:]
    else:
        stderr = plain_errors(stderr)
    value = {'via': 'ssh', 'exitCode': raw.get('exitCode'), 'stdout': stdout, 'stderr': stderr, 'truncated': bool(raw.get('truncated')),
             'timedOut': bool(raw.get('timedOut')), **({'shell': shell} if shell else {})}
    if raw.get('timedOut'):
        return {**value, 'ok': False, 'effectState': 'unknown', 'outcome': 'timeout',
                'note': 'SSH timed out; the command may still be running on the server. Check before running it again.'}
    if raw.get('exitCode') == 255:
        if not started and _NOT_STARTED.search(stderr):
            return {**value, 'ok': False, 'error': 'ssh_connection_failed', 'effectState': 'not_started', 'outcome': 'refused'}
        return {**value, 'ok': False, 'error': 'ssh_result_unknown', 'effectState': 'unknown', 'outcome': 'unknown',
                'note': 'The SSH connection ended without a result (or the command exited 255); it may have run, fully or partly. '
                        'Check before running it again.'}
    return {**value, 'ok': raw.get('exitCode') == 0, 'effectState': 'observed', 'outcome': 'ok' if raw.get('exitCode') == 0 else 'failed'}
