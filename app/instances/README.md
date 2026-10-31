# Controller instances integration

## Runtime owner calls

`host` below is the **WebAdminServer**, not a second Services singleton. It must
already have its normal DB, tools, run scheduler, model factory and conversation
methods. Retain the same service on Services if lifecycle ownership requires it.

```python
from app.instances import InstanceService, install
from app.tools.instances import register_instance_tools

# Controller mode only, once DB and Web host dependencies are available:
host.instances = InstanceService(db, host)
await host.instances.start()
register_instance_tools(registry, host.instances)
# Before app.freeze / runner setup:
install(app, host, host.instances)
# Before DB/host dependencies close:
await host.instances.close()
```

The controller has no instance event endpoint to let through its authentication:
instance events are pulled from the access gateway (`GET /control/v1/events`,
authenticated by the gateway control key), not pushed to the controller. All `/api/instances` handlers independently
require the controller role and the existing authenticated owner session/whitelist.

`chat_runtime.py` owns `prepare_turn`, `bind_context` → `bind_run`, and `finish`.
Runtime owner need not change that file. Ordinary Web/Cron/Webhook/recommendation
turns have no instance capability merely because their origin says deployment.
The exact persisted conversation/chat/owner, active operation, fresh revision,
root turn and actual runtime run ID must all match. Copied/branched conversations
inherit no capability or instance secret expansion. Deleting a conversation
never removes an instance. A human follow-up gets a fresh binding and current
protected profile, not an automatic redeployment instruction.

## Storage and private material

Module schema initializes in `start`; main DB schema/engine are unchanged.
Instances and operation history have no cascading conversation foreign key.
SSH data, initial model configuration, generated management/report keys and
pinned SSH host keys are encrypted with the existing reference-material cipher.
Back up the DB **and its adjacent `reference-materials.key` (0600)** together.
Public instance/event/error responses never include the profile or credentials.

Actual authorized credentials enter the configured provider through the
existing request-only encrypted reference overlay. Original inputs are not
masked. Instance material expands only for the exact current conversation,
operation, bundle and execution Task, not context inspection, copies or Agents.
Bound output is buffered per physical model call before public projection, so
split credential echoes cannot leak through streaming; known secret echoes are
filtered in public text/audit projections. Credential-bearing model tool
arguments are rejected before execution rather than running fake credentials.
Other turns retain their original streaming behavior. This is accidental-leak
protection, not isolation from a trusted controller OS user or a hostile model
with unrestricted existing file/network tools.

## Complete deployment and maintenance path

1. `POST /api/instances` saves a draft; secrets omitted from PATCH preserve the
   original values. Initial child account password is 1–1024 characters (the UI
   only advises on strength).
   `initialConfig` uses the real child business configuration schema, including
   valid `models.providers` and `models.primary` for a new workspace.
2. `POST /{id}/deploy` creates **and starts** a dedicated ordinary Web execution.
   `POST /{id}/maintenance` requires explicit instructions and starts a distinct
   maintenance operation. These authenticated submissions are user scope;
   model side effects still use the existing human confirmation protocol. Tool
   approval also binds the instance revision and shows the fixed SSH/management
   target; form changes while confirmation waits invalidate that approval.
3. `InstanceControl(action=describe)` documents the tool. `status` with no ID
   returns the exact bound operation and revision. `remote` runs a command only
   on the recorded target, without asking the user per command (the model owns
   keeping it within the deployment or stated maintenance): on the controller's
   own machine directly with a sanitized environment; on another server through
   its node (as root) while the node is online, else over SSH as the login user.
   The result names the channel used. SSH does not inherit SSH configuration,
   agent keys or parent environment. Optional `sudo:true` (SSH password logins
   only) sends the stored target password via stdin, not tool arguments. SSH
   first connection explicitly uses TOFU
   (`accept-new`); the pinned host key is retained encrypted; changed keys fail.
   Controller requires an existing OpenSSH client; no software is auto-installed.
4. No published official Docker image is assumed. A user can supply a verified
   existing reference, or explicitly authorize building a particular same-repo
   source revision with its Dockerfile on the target / transferring an image.
   Current release ZIP is not a full source build context. Model may adapt the
   target's Docker environment through confirmed `remote` commands. No real
   image publication is part of this implementation.
5. `deliver` takes the current revision and optional verified/built image tag;
   port and host bind addresses come from the record (loopback, plus the mesh
   address for a gateway on another server, with a boot unit that starts the
   container once that address is up; SSH-only servers `0.0.0.0`). Containers
   get `pids_limit` and the optional per-instance memory limit. It privately transfers actual generated
   keys, seeds and business configuration to `.deploy-<directory name>` beside the
   data directory (not inside it) as 0600 files. Compose maps the selected host port to **18961**, with only the user's
   dedicated host directory mounted at `/workspace`. It never mounts a Docker
   socket or controller/other-tenant state. No image pull or container start is
   hidden in delivery.
6. By default delivery validates saved initial model config and creates
   `<workspacePath>/openbear.json` **only if absent**, with Web binding
   `0.0.0.0:18961`; it never overwrites existing volume data. `initialize:false`
   is for an already verified valid volume, not a missing-config workaround.
   `apply_initial_config` performs an explicitly confirmed management PATCH for
   later business changes. Actual Docker startup/rebuild must then be executed
   and inspected by the model; generating commands or delivery alone is not
   completion. Image/port/target/SSH key and credential deployment records are
   retained for maintenance.
7. `report` requires current revision. Controller separately authenticates
   `/api/managed/v1/status`, checks exact identity, ready, `/workspace` paths and
   **`persistence.mounted is true`**, then probes the configured user route.
   Every public origin must return the actual child's
   `X-OpenBear-Instance` identity header on `/api/auth/mode` (JSON stays `{ok,mode}`).
   OpenResty passes that upstream header and never synthesizes identity; a route
   accidentally pointing to another managed runtime cannot pass. Ready is distinct
   from actual `health.status`; overall verification additionally requires exactly
   `healthy`. Degraded/unknown preserve their health issues but stay
   `needs_verification`, including operation completion and notification state.
   A report does not close the run or grant a late reporting right.
8. No report or abnormal terminal → interrupted/needs_verification, even if a
   probe succeeds. Never assume remote effects absent. Restart only verifies
   interrupted/reported operations and delivers stable-key notifications;
   it never runs deployment commands or starts another model conversation.

Management relay only accepts explicit child business paths, fixed stored
origins and methods. Paths are strictly UTF-8 decoded once per segment and
canonically re-encoded, preserving Chinese names and encoded opaque model IDs
without treating their encoded slash as a route separator. Actual child business
field validation still applies (e.g. channel mutation's ASCII-name restriction).
It rejects malformed/double encoding, encoded traversal, absolute URLs, custom
headers/hosts, auth/controller/updater paths, query/fragment smuggling,
redirects, oversized responses and JSON nested deeper than 64 levels. TLS
verification is always on; HTTP is not restricted to private addresses.
Management and gateway control calls to a server whose node is connected and
advertises `http.local` go through that node to `127.0.0.1:<port>` over the
encrypted node channel (loopback only, 2 MB / 60 s caps); new instances there
listen on loopback, plus their mesh address when the gateway is on another mesh
member (`POST /{id}/migrate-network` moves older ones). SSH-only servers keep
server address + port. Model mutations
(including management jobs/configuration) always confirm. Managed children and
Agent scope never register `InstanceControl`.

## Independent OpenResty routing

The controller does not export route files (there is no
`/api/instances/{id}/route`); public access normally goes through the access
gateway, which the controller keeps in sync. For an independently run OpenResty, install the provided
`openresty-common.conf` map/default-deny blocks once, and route files in the
independently running OpenResty `http {}` context. Resolve existing 1Panel/default
listeners, the explicit certificate paths and CA bundle path during an
authorized deployment/maintenance operation. Nonstandard public listeners need
the same deny-default policy. If serving IPv6, add corresponding IPv6 listeners
and deny-default blocks. Never alter actual 1Panel from development.

Routes require exact registered Host (no wildcard fallback), preserve streaming,
uploads and WebSocket upgrades, validate upstream TLS and contain no management
Bearer. Management paths are not publicly proxied. The family data path is
OpenResty → child, not the controller Python/conversation worker. Install/reload
and external reachability are separate facts; exported material alone is not
an active route. Archive disables registry/report/relay/export authority, keeps
history and all remote data, and **does not automatically remove an already
installed route**. Explicitly retire its owned config and reload only with
separate authorization; reconcile stale files when reusing a hostname.

## Verification

`tests/test_instances.py` uses real isolated SQLite, aiohttp child lifecycle,
actual Web/Agent execution with a deterministic provider, real isolated
bootstrap shell/subprocess transport, encrypted originals vs public logs,
report revision/concurrency/late guards, copy/deletion isolation, no-report and
restart behavior, notification keys, role/relay rejection and route materials.
No real household SSH/Docker deployment, public publish or service reload occurs.
