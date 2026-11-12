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


STUCK = '''
import asyncio
import pytest


async def never():
    await asyncio.Event().wait()


@pytest.fixture
async def closing():
    yield
    await asyncio.gather(asyncio.create_task(never(), name='never-ends'))


async def test_stuck(closing):
    pass


def test_after():
    pass
'''


def test_a_stuck_test_process_is_ended_with_its_tasks_and_the_run_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(check, 'STALL_S', 2)
    (tmp_path / 'test_case.py').write_text(STUCK)
    (tmp_path / 'pytest.ini').write_text('[pytest]\nasyncio_mode = auto\n')
    progress, log, stuck = tmp_path / 'progress', tmp_path / 'p.log', []
    progress.mkdir()
    env = {**check.base_env(), 'PYTHONPATH': str(check.ROOT), 'CHECK_PROGRESS_DIR': str(progress)}
    cmd = check.pytest_cmd(tmp_path / 'j.xml', 2, str(tmp_path / 'test_case.py'), '--rootdir', str(tmp_path), '-c', str(tmp_path / 'pytest.ini'))
    assert check.run(cmd, check.ROOT, env, log, check.time.monotonic() + 90, progress=progress, stuck=stuck) is not None
    assert stuck == ['test_case.py::test_stuck（teardown）']
    text = log.read_text()
    # The stuck task down to the line it waits on, not just its outermost frame.
    assert '卡住的测试进程' in text and 'never-ends' in text and 'test_case.py:7 never' in text
    lane = check.Lane('python')
    check.read_junit(tmp_path / 'j.xml', lane, tmp_path)
    assert lane.passed == 1 and [f.title for f in lane.failures] == ['test_case.py::test_stuck']


def test_a_run_ended_by_the_deadline_reports_what_it_finished(tmp_path):
    log = tmp_path / 'p.log'
    # pytest -q under xdist, killed in the middle of a progress line.
    log.write_text('8 workers [150 items]\n\n' + '.' * 72 + ' [ 50%]\n.F.')
    assert check._counted(log) == (75, 150)
