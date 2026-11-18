"""The mesh's alerts by type in the reminder settings (2026-11-16: a backup entry on a broken relay
alerted again and again with no way to stop it): each on by default, one turned off is listed with
status off and not sent, and turning one off issues no mesh revision. Real Web app, isolated
database, fake nodes."""
from __future__ import annotations

import pytest

from app.instances.contracts import InstanceError
from app.servers import mesh_status, reminders
from app.webhooks.repository import many
from tests.test_mesh_missing import gone_nodes, meshed  # noqa: F401
from tests.test_mesh_service import OWNER, connect, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def test_mesh_alerts_are_on_by_default_merged_and_checked():
    assert reminders.clean({})['mesh'] == dict.fromkeys(reminders.MESH_ALERTS, True)
    # Settings from before: every type on; a stored value that is no bool is on again.
    assert reminders.clean({}, {'enabled': False})['mesh'] == dict.fromkeys(reminders.MESH_ALERTS, True)
    assert reminders.clean({}, {'mesh': {'standby': 'no', 'flap': False}})['mesh'] == {**dict.fromkeys(reminders.MESH_ALERTS, True), 'flap': False}
    off = reminders.clean({'mesh': {'standby': False}}, {'mesh': {'flap': False}})
    assert off['mesh'] == {**dict.fromkeys(reminders.MESH_ALERTS, True), 'standby': False, 'flap': False}
    assert off['enabled'] is True
    for bad in ({'mesh': {'nope': False}}, {'mesh': {'standby': 0}}, {'mesh': []}):
        with pytest.raises(InstanceError) as exc:
            reminders.clean(bad)
        assert exc.value.error == 'invalid_mesh_alerts'
    # Every alert the mesh raises has a type the settings name, and every type one alert.
    assert set(mesh_status.ALERT_TYPES.values()) == set(reminders.MESH_ALERTS)
    assert mesh_status.alert_type('mesh_standby_unhealthy:m1:1') == 'standby'


async def test_a_mesh_alert_turned_off_is_listed_not_sent_and_no_revision(env):  # noqa: F811
    e = env
    nodes, ids, gone = await meshed(e)
    revisions = (await api(e, 'GET', '/api/servers/mesh/revisions'))['revisions']
    settings = (await api(e, 'PUT', '/api/servers/settings', {'reminders': {'mesh': {'missing': False}}}))['settings']
    assert settings['reminders']['mesh'] == {**dict.fromkeys(reminders.MESH_ALERTS, True), 'missing': False}
    assert (await api(e, 'GET', '/api/servers/mesh/revisions'))['revisions'] == revisions
    assert (await api(e, 'GET', '/api/servers/mesh'))['draft']['dirty'] is False
    assert (await api(e, 'PUT', '/api/servers/settings', {'reminders': {'mesh': {'missing': 'off'}}}, status=400))['error'] == 'invalid_mesh_alerts'

    gone.add(ids['m1'])
    await connect(e, ids['m1'])
    await settled(e)
    rows = await many(e.db.conn, "SELECT id, status FROM server_notifications WHERE kind='mesh'")
    assert [(r['id'].split(':')[0], r['status']) for r in rows] == [('mesh-missing', 'off')]

    # On again: the next one goes out.
    await api(e, 'PUT', '/api/servers/settings', {'reminders': {'mesh': {'missing': True}}})
    await e.servers.mesh._remind(OWNER, f'mesh-missing:{ids["m2"]}:1', ids['m2'], '组网：测试')
    rows = await many(e.db.conn, "SELECT status FROM server_notifications WHERE id=?", (f'mesh-missing:{ids["m2"]}:1',))
    assert [r['status'] for r in rows] == ['pending']
