"""Server nodes as browser hosts for the Browser tool. Main controller only:
the node's browser carries that machine's logins and network."""

from __future__ import annotations

import json

from app.webhooks.repository import many, one


class NodeLink:
    def __init__(self, hub, server_id):
        self.hub, self.server_id = hub, server_id

    def session(self):
        return self.hub.sessions.get(self.server_id)

    async def call(self, method, params, timeout):
        return await self.hub.call(self.server_id, method, params, timeout)


class NodeBrowsers:
    def __init__(self, service):
        self.service = service
        self.links = {}
        # Whether any node offers a browser: shows the Browser tool without a local browser.
        self.any_browser = False
        self.on_change = None

    def link(self, server_id):
        if server_id not in self.links:
            self.links[server_id] = NodeLink(self.service.hub, server_id)
        return self.links[server_id]

    async def owner(self, ctx):
        if ctx.agent_session_uuid or ctx.task_uuid:
            raise ValueError("node_browser_main_controller_only")
        row = await one(self.service.db.conn, 'SELECT owner_chat_id FROM web_conversations WHERE conversation_uuid=? AND internal_chat_id=?',
                        (ctx.conversation_uuid, ctx.chat_id))
        if not row or row['owner_chat_id'] not in {int(v) for v in self.service.host.config.telegram.whitelist_ids}:
            raise ValueError("node_browser_owner_required")
        return row['owner_chat_id']

    async def available(self, owner):
        """Servers whose node has the browser capability, with their current state."""
        names = {i['id']: i['name'] for i in await self.service.registry.all(owner)}
        out = []
        for row in await many(self.service.db.conn, 'SELECT server_id, info_json FROM server_nodes WHERE owner=?', (owner,)):
            session = self.service.hub.sessions.get(row['server_id'])
            caps = session.caps if session else json.loads(row['info_json']).get('caps', {})
            if caps.get('browser') and row['server_id'] in names:
                out.append({'node': names[row['server_id']], 'serverId': row['server_id'], 'online': bool(session)})
        return out

    async def resolve(self, ctx, value):
        """A server id or its exact name (case-insensitive) with the browser capability."""
        owner = await self.owner(ctx)
        rows = await self.available(owner)
        wanted = str(value).strip().lower()
        match = [r for r in rows if r['serverId'] == value] or [r for r in rows if r['node'].lower() == wanted]
        if not match:
            raise ValueError("node_browser_not_found: list nodes with status")
        if len(match) > 1:
            # Server names need not be unique; never pick one machine's browser by guess.
            raise ValueError("node_browser_ambiguous: several servers have this name; use its serverId from status")
        if not match[0]['online']:
            raise ValueError("node_offline")
        return match[0]

    async def refresh(self):
        found = False
        for row in await many(self.service.db.conn, 'SELECT server_id, info_json FROM server_nodes', ()):
            session = self.service.hub.sessions.get(row['server_id'])
            if (session.caps if session else json.loads(row['info_json']).get('caps', {})).get('browser'):
                found = True
                break
        if found != self.any_browser:
            self.any_browser = found
            if self.on_change:
                self.on_change()
