"""OPS-F05 serialization is additive and does not invent N-1 capabilities."""
import pytest

from app.servers.service import ServerService


@pytest.mark.parametrize('reported,expected', [(True, True), (False, False), (None, None), ('true', None), (1, None)])
def test_job_view_preserves_only_explicit_boolean_stop_capability(reported, expected):
    view = ServerService._job_view({'id': 'orphan01', 'state': 'interrupted', 'canStop': reported})
    assert view['state'] == 'interrupted'
    assert view['canStop'] is expected


def test_n_minus_one_job_does_not_claim_residual_process_support():
    for state in ('running', 'interrupted', 'exited'):
        assert ServerService._job_view({'id': 'legacy01', 'state': state})['canStop'] is None
