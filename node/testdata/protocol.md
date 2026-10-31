# Controller ↔ node protocol samples

Shared by both sides' tests; nothing here is read by the node or the controller themselves.

- `node/protocol_samples_test.go` checks the real node against every sample: what it sends
  (auth, hello, report, replies, enrollment) matches, and what the controller sends decodes
  into the node's own types without unknown fields and gets the replies shown.
- `tests/fake_node.py` (through `tests/node_protocol.py`) is the node in the controller's tests:
  it checks every message and call the controller sends against these samples, and what it
  sends back against them too.

So a change of the protocol on either side fails that side's tests until this file says the
same, and then the other side's tests show whether it still fits.

The file is Markdown so the node's public source policy takes it (`node/` holds `.go` and
`.md` files); `go build` and `go test ./...` skip `testdata` directories.

## Notation

Each sample is a `### name` heading followed by one ```` ```json ```` block.

- `"<string>"`, `"<int>"`, `"<int?>"` (an integer or null), `"<number>"`, `"<bool>"`,
  `"<base64>"`, `"<hex64>"` (64 lowercase hex digits), `"<any>"`: a value of that type.
- `"<ref:name>"`: the sample called name.
- A key ending in `?` may be left out; every other key must be there, and no other key may.
- `["<each>", p]`: a list of any length whose items all match p. Any other list is that list.
- Other values must be equal. Where a test only has an example to go by (call parameters, or
  the fake node's results for its own commands) it compares types instead.
- `<dir>`, `<url>`, `<url2>`, `<job>`, `<binary>`, `<build>` are filled in by the tests: a
  temporary directory, the controller's address and a second one for it, a job id from
  `job.start`, this platform's binary name and the running build's sha256.

`call:METHOD` samples are `{"params": example, "result": pattern}` or
`{"params": example, "error": code}`; `call:METHOD/variant` are more cases of one method.
`fs.edit` samples also give the file before (`file` or `fileBase64`) and after (`after`) the
call, and their results exactly, so the fake node can answer with them.

## Handshake (plain WebSocket text frames)

The controller opens with a challenge; the node answers with its signed auth, its handshake
parameters as signed JSON text in `params` and plain copies beside it for older controllers.

### msg:challenge
```json
{"t": "challenge", "nonce": "<string>", "eph": "<base64>"}
```

### msg:auth
```json
{"t": "auth", "node": "<string>", "eph": "<base64>", "sig": "<base64>", "params": "<string>", "paramsSig": "<base64>",
 "version": "<string>", "protocol": "<int>", "protocolMin": "<int>", "notices": true, "probe?": true}
```

### msg:auth.params
The JSON text in auth.params. `probe` marks a connection that only proves an address works.
```json
{"version": "<string>", "protocol": "<int>", "protocolMin": "<int>", "notices": true, "probe?": true}
```

### msg:welcome
```json
{"t": "welcome", "sig": "<base64>", "paramsSig": "<base64>"}
```

## Session (AES-GCM sealed binary frames: kind 0 JSON, kind 1 terminal bytes)

A terminal frame is kind 1, then the session id as 4 bytes big-endian, then the bytes.

### msg:config
The controller's first sealed message.
```json
{"t": "config", "interval": "<number>", "protocol": "<int>", "protocolMin": "<int>", "signedParams": true, "probe?": true}
```

### info
```json
{"hostname": "<string>", "platform": "<string>", "os": "<string>", "kernel": "<string>", "arch": "<string>", "cpu": "<string>",
 "cores": "<int>", "virtualization": "<string>", "memTotal": "<int>", "swapTotal": "<int>", "diskTotal": "<int>",
 "ipv4": "<string>", "ipv6": "<string>", "ipv4Proxy?": "<string>", "machineId": "<string>"}
```

### msg:hello
The node's first sealed message, and again when its addresses or browser change.
```json
{"t": "hello", "info": "<ref:info>", "caps": {"exec": "<bool>", "terminal": "<bool>", "browser": "<bool>"},
 "version": "<string>", "build": "<hex64>", "endpoint": "<string>", "backup": "<bool>",
 "features": ["fs.edit", "jobs", "exec.shell", "mesh", "net", "access.mesh", "net.ports", "ddns", "http.local", "mesh.confirmed", "fs.transfer"],
 "protocol": 1, "protocolMin": 1}
```

### msg:report
Every config.interval seconds. `mesh` only on a mesh member.
```json
{"t": "report", "m": {"cpu": "<number>", "steal": "<number>", "iowait": "<number>",
  "mem": {"total": "<int>", "used": "<int>"}, "swap": {"total": "<int>", "used": "<int>"},
  "load": ["<number>", "<number>", "<number>"], "disk": {"total": "<int>", "used": "<int>"},
  "io": {"readRate": "<int>", "writeRate": "<int>"}, "net": {"rx": "<int>", "tx": "<int>", "rxRate": "<int>", "txRate": "<int>"},
  "tcp": "<int>", "udp": "<int>", "processes": "<int>", "uptime": "<int>", "boot": "<int>", "nodeRss": "<int>"},
 "mesh?": "<any>"}
```

### msg:call
```json
{"t": "call", "id": "<string>", "method": "<string>", "params": "<any>"}
```

### msg:reply
```json
{"t": "reply", "id": "<string>", "ok": true, "result": "<any>"}
```

### msg:reply/error
```json
{"t": "reply", "id": "<string>", "ok": false, "error": "<string>"}
```

### msg:close
A refusal inside the channel (4403 revoked), which a node takes as authentic.
```json
{"t": "close", "code": "<int>", "reason": "<string>"}
```

### msg:term.open
```json
{"t": "term.open", "sid": "<int>", "cols": "<int>", "rows": "<int>"}
```

### msg:term.opened
```json
{"t": "term.opened", "sid": "<int>"}
```

### msg:term.resize
```json
{"t": "term.resize", "sid": "<int>", "cols": "<int>", "rows": "<int>"}
```

### msg:term.ack
The controller has passed on n more bytes of the terminal's output.
```json
{"t": "term.ack", "sid": "<int>", "n": "<int>"}
```

### msg:term.close
```json
{"t": "term.close", "sid": "<int>"}
```

### msg:term.closed
reason: exited, closed (after term.close), capability_disabled, start_failed, too_many_terminals, node_disconnected, input_overflow.
Input is queued without blocking the session loop. A terminal is closed with input_overflow when
its queued input exceeds 1 MiB or its 1024-frame input queue is full; these are separate limits.
```json
{"t": "term.closed", "sid": "<int>", "reason": "<string>"}
```

## Enrollment (HTTP POST /api/server-nodes/v1/enroll)

### enroll:request
```json
{"code": "<string>", "publicKey": "<base64>", "info": "<ref:info>", "version": "<string>", "protocol": "<int>", "protocolMin": "<int>"}
```

### enroll:response
```json
{"ok": true, "nodeId": "<string>", "controllerKey": "<base64>"}
```

### enroll:error
```json
{"ok": false, "error": "<string>"}
```

## Calls

### call:ping
```json
{"params": {}, "result": {"version": "<string>"}}
```

### call:exec
```json
{"params": {"command": "pwd; cat; printf '%s' \"$((6*7))\" >&2; exit 2", "timeout": 30, "cwd": "<dir>", "stdin": "hi", "shell?": "sh"},
 "result": {"exitCode": 2, "stdout": "<dir>\nhi", "stderr": "42", "truncated": false, "timedOut": false, "durationMs": "<int>", "shell": "/bin/sh"}}
```

### call:exec/timeout
```json
{"params": {"command": "sleep 5", "timeout": 1, "cwd": "", "stdin": "", "shell": "sh"},
 "result": {"exitCode": -1, "stdout": "", "stderr": "", "truncated": false, "timedOut": true, "durationMs": "<int>", "shell": "/bin/sh"}}
```

### call:fs.write
One chunk of an upload (the controller always sends uploads); the file appears when done.
```json
{"params": {"path": "<dir>/upload.txt", "data": "aGVsbG8=", "upload": "abcdef123456", "offset": 0, "done": true, "mode": "", "mkdirs": false},
 "result": {"path": "<dir>/upload.txt", "size": 5, "done": true}}
```

### call:fs.write/partial
```json
{"params": {"path": "<dir>/sub/part.txt", "data": "aGVs", "upload": "abcdef654321", "offset": 0, "done": false, "mode": "0600", "mkdirs": true},
 "result": {"path": "<dir>/sub/part.txt", "size": 3, "done": false}}
```

### entry
```json
{"name": "<string>", "type": "<string>", "dir?": "<bool>", "size": "<int>", "mode": "<string>", "mtime": "<int>", "target?": "<string>"}
```

### call:fs.stat
```json
{"params": {"path": "<dir>/upload.txt"},
 "result": {"name": "upload.txt", "type": "file", "dir?": true, "size": 5, "mode": "0644", "mtime": "<int>", "target?": "<string>"}}
```

### call:fs.list
```json
{"params": {"path": "<dir>"},
 "result": {"path": "<dir>", "entries": ["<each>", "<ref:entry>"], "truncated": false}}
```

### call:fs.read
```json
{"params": {"path": "<dir>/upload.txt", "offset": 1, "limit": 1048576},
 "result": {"data": "ZWxsbw==", "size": 5, "offset": 1, "read": 4, "eof": true, "mode": "0644", "mtime": "<int>"}}
```

### call:fs.read/missing
```json
{"params": {"path": "<dir>/nope", "offset": 0, "limit": 10}, "error": "not_found"}
```

`fs.transfer` is an optional feature. It adds `fs.transfer` (the chunked `fs.write` fields, plus `noReplace`; the final call requires `size` and lowercase `sha256`), `fs.hash` (regular-file size and SHA-256), and `fs.cancel` (path and upload ID). The final transfer checks the temporary file before publishing it; `noReplace` refuses even a destination created during the upload. A failed digest leaves the previous destination intact. Cancellation only removes that upload's temporary file, never a committed destination. Older nodes keep their existing file methods; controllers require this feature for verified path-to-path transfers.

### call:fs.edit
```json
{"file": "listen 80;\nroot /srv/a;\n# a comment\nroot /srv/a;\n",
 "params": {"path": "<dir>/site.conf", "edits": [{"old": "listen 80;", "new": "listen 8080;", "replaceAll": false},
                                                {"old": "root /srv/a;", "new": "root /srv/b;", "replaceAll": true}]},
 "result": {"path": "<dir>/site.conf", "replacements": 3, "size": 51, "changed": true, "diffTruncated": false,
            "diff": "--- <dir>/site.conf\n+++ <dir>/site.conf\n@@ -1,4 +1,4 @@\n-listen 80;\n+listen 8080;\n-root /srv/a;\n+root /srv/b;\n # a comment\n-root /srv/a;\n+root /srv/b;\n"},
 "after": "listen 8080;\nroot /srv/b;\n# a comment\nroot /srv/b;\n"}
```

### call:fs.edit/failures
Nothing is written when any edit fails.
```json
{"file": "listen 8080;\nroot /srv/b;\n# a comment\nroot /srv/b;\n",
 "params": {"path": "<dir>/site.conf", "edits": [{"old": "listen 8080;", "new": "listen 1;", "replaceAll": false},
                                                {"old": "missing", "new": "x", "replaceAll": false},
                                                {"old": "root /srv/b;", "new": "y", "replaceAll": false},
                                                {"old": "8080;\nroot", "new": "z", "replaceAll": false}]},
 "result": {"path": "<dir>/site.conf", "changed": false, "failures": [
   {"edit": 2, "reason": "not_found"}, {"edit": 3, "reason": "multiple", "count": 2, "lines": [2, 4]}, {"edit": 4, "reason": "overlap", "with": 1}]},
 "after": "listen 8080;\nroot /srv/b;\n# a comment\nroot /srv/b;\n"}
```

### call:fs.edit/crlf
Text with \n matches a file written with \r\n, and the new text gets \r\n too.
```json
{"file": "[a]\r\nkey=1\r\nother=2\r\n",
 "params": {"path": "<dir>/win.ini", "edits": [{"old": "key=1\nother=2", "new": "key=9\nother=8\nadded=1", "replaceAll": false}]},
 "result": {"path": "<dir>/win.ini", "replacements": 1, "size": 30, "lineEndings": "crlf", "changed": true, "diffTruncated": false,
            "diff": "--- <dir>/win.ini\n+++ <dir>/win.ini\n@@ -1,3 +1,4 @@\n [a]\n-key=1\n-other=2\n+key=9\n+other=8\n+added=1\n"},
 "after": "[a]\r\nkey=9\r\nother=8\r\nadded=1\r\n"}
```

### call:fs.edit/not_text
```json
{"fileBase64": "//4AYWJj", "params": {"path": "<dir>/blob.bin", "edits": [{"old": "abc", "new": "x", "replaceAll": false}]},
 "error": "not_text", "after": null}
```

### job
```json
{"id": "<string>", "command": "<string>", "cwd?": "<string>", "shell": "<string>", "state": "<string>", "exitCode": "<int?>",
 "signal?": "<string>", "startedAt": "<int>", "endedAt?": "<int>", "durationMs": "<int>", "pid?": "<int>"}
```

### call:job.start
```json
{"params": {"command": "echo begin; sleep 1; echo end; exit 3", "cwd": "", "stdin": ""}, "result": "<ref:job>"}
```

### call:job.list
```json
{"params": {}, "result": {"jobs": ["<each>", "<ref:job>"]}}
```

### call:job.status
Without offset, the last limit bytes; with it, from there (offsets count every byte written).
```json
{"params": {"id": "<job>", "offset?": 0, "limit?": 65536},
 "result": {"job": "<ref:job>", "log": {"data": "<string>", "offset": "<int>", "next": "<int>", "size": "<int>", "first": "<int>"}}}
```

### call:job.stop
```json
{"params": {"id": "<job>"}, "result": {"job": "<ref:job>", "stopped": "<bool>"}}
```

### call:update
The node downloads /api/server-nodes/v1/download/NAME from the address in use, checks size and
sha256, replaces itself and reconnects running it; for the build it already runs, nothing changes.
```json
{"params": {"version": "1.2.0", "name": "<binary>", "sha256": "<build>", "size": 1234},
 "result": {"from": "<string>", "to": "<string>", "changed": "<bool>"}}
```

### call:config.url
The node proves the new address with a probe handshake before saving it, then reconnects there.
```json
{"params": {"url": "<url2>", "backup": "<url>"}, "result": {"url": "<url2>", "backup": "<url>", "backupReachable": true, "changed": true}}
```

### call:config.url/unchanged
```json
{"params": {"url": "<url>", "backup": ""}, "result": {"url": "<url>", "backup": "", "changed": false}}
```

### call:uninstall
The node replies, then removes itself and exits.
```json
{"params": {}, "result": {"version": "<string>"}}
```

## Browser calls

The node runs the browser engine next to its browser; the controller's Browser tool drives it
with these. Without the browser capability every one but browser.config and browser.status is
refused with capability_disabled.

### browser
```json
{"enabled": "<bool>", "state": "<string>", "source": "<string>", "product": "<string>", "headless": "<bool>", "epoch": "<string>",
 "endpoint": "<string>", "download": "<bool>", "downloadsUnavailable?": true, "error?": "<string>", "downloaded?": "<int>"}
```

### call:browser.config
```json
{"params": {"enabled": true, "endpoint": "ws://127.0.0.1:1/devtools/browser/none", "download": false}, "result": "<ref:browser>"}
```

### call:browser.status
```json
{"params": {}, "result": "<ref:browser>"}
```

### call:browser.start
A browser that cannot be started is a state, not an error.
```json
{"params": {}, "result": "<ref:browser>"}
```

### call:browser.restart
```json
{"params": {}, "result": "<ref:browser>"}
```

### call:browser.call/not_started
```json
{"params": {"action": "snapshot", "params": {}}, "error": "browser_not_started"}
```

### call:browser.call
An engine action. Its failure is a result too, as the local worker reports it: error, outcome, details.
Its own params are the engine's (test_browser_engine_parity holds the two engines together), so the
fake node checks only the envelope; so for browser.cdp's params.
outputPath "*" has the node write the artifact into its own folder; paths must be its uploads.
```json
{"params": {"action": "configure", "params": {"snapshotMaxChars": 1000, "maxEventEntries": 10, "maxBodyBytes": 1000, "maxArtifactBytes": 1000}},
 "result": {"result?": "<any>", "error?": "<string>", "outcome?": "<string>", "details?": "<any>"}}
```

### call:browser.cdp
Raw CDP the controller's service sends itself; with target, a call on that page.
```json
{"params": {"method": "Browser.getVersion", "params": {}, "target?": "<string>"}, "result": {"result?": "<any>", "error?": "<string>", "epoch": "<string>"}}
```

### call:browser.cdp/not_forwarded
```json
{"params": {"method": "Page.navigate", "params": {}}, "error": "method_not_forwarded"}
```

### call:browser.upload
One chunk of a file for a page's file chooser; the last one (done) tells its path on the node.
```json
{"params": {"upload": "abcdefgh12", "name": "report.txt", "offset": 0, "data": "aGk=", "done": true}, "result": {"path?": "<string>"}}
```

### call:browser.artifact
A file the engine wrote, in chunks; the last read removes it.
```json
{"params": {"path": "<string>", "offset": 0, "limit": 4194304}, "result": {"data": "<base64>", "eof": "<bool>", "size": "<int>"}}
```

### call:browser.artifact/outside
```json
{"params": {"path": "/etc/passwd", "offset": 0, "limit": 10}, "error": "invalid_params"}
```

## Other

### enroll:fingerprint
What an install command's --fingerprint (or OPENBEAR_CONTROLLER_FINGERPRINT) holds: the sha256 of
the controller's identity key as enroll:response carries it, in lowercase hex; the node also takes
it in uppercase.
```json
{"controllerKey": "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=", "fingerprint": "56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c"}
```

### call:no.such.method
```json
{"params": {}, "error": "unsupported_method"}
```

### refusal:capability_disabled
A node whose owner turned command execution off refuses these calls with capability_disabled.
The list below is the shared executable fixture, not an exhaustive list of methods. The node also
requires command execution for http.local, browser.config and the implemented mesh methods
(status, install, apply, tls, upgrade, uninstall, prepare, activate, confirm, abort).
```json
["exec", "update", "fs.list", "fs.stat", "fs.read", "fs.write", "fs.edit", "job.start", "job.list", "job.status", "job.stop"]
```
