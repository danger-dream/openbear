from types import SimpleNamespace

import pytest

from scripts import check


@pytest.mark.parametrize('rel, before, after', [
    ('pyproject.toml', b'[project]\nname="openbear"\nversion="1.0.0-dev.1"\ndependencies=["aiohttp"]\n', b'[project]\nname="openbear"\nversion="1.0.0-dev.2"\ndependencies=["aiohttp"]\n'),
    ('uv.lock', b'version=1\n[[package]]\nname="openbear"\nversion="1.0.0.dev1"\nsource={virtual="."}\n', b'version=1\n[[package]]\nname="openbear"\nversion="1.0.0.dev2"\nsource={virtual="."}\n'),
])
def test_only_own_version_bumps_are_ignored(rel, before, after):
    assert check._digest(rel, before) == check._digest(rel, after)
    assert check._digest(rel, before) != check._digest(rel, after + b'changed=true\n')


def test_dependency_version_changes_are_not_ignored():
    before = b'version=1\n[[package]]\nname="aiohttp"\nversion="3.0.0"\n'
    assert check._digest('uv.lock', before) != check._digest('uv.lock', before.replace(b'3.0.0', b'3.1.0'))
    assert {'pyproject.toml', 'uv.lock', 'benchmark-package'} <= set(check.SCOPES)


@pytest.mark.parametrize('path', ['pyproject.toml', 'uv.lock', 'benchmark-package/ip.check.place/v0.1.0/ip.sh'])
def test_changed_dependencies_and_benchmark_resources_select_checks(tmp_path, monkeypatch, path):
    monkeypatch.setattr(check, 'STATE', tmp_path)
    (tmp_path / 'testmondata').touch()
    py = check.PyIndex()
    web = SimpleNamespace(tests=[])
    plan = check.make_plan(SimpleNamespace(full=False, base='HEAD'), {'snapshot': {path: 'old'}}, {path: 'new'}, web, py)
    if path in check.PY_DEPENDENCIES:
        assert plan.python and not plan.extra
    else:
        assert not plan.python and set(plan.extra) == {'tests/test_servers_bench.py', 'tests/test_bench_review.py', 'tests/test_release_resources.py'}
    assert not plan.uncovered and not plan.empty()


def test_failed_python_dependency_validation_does_not_settle(tmp_path):
    plan = check.Plan(first=False, changed={'uv.lock'})
    lane = check.Lane('python')
    lane.complete = False
    assert 'uv.lock' not in check.settle(plan, SimpleNamespace(), {'python': lane}, {})
