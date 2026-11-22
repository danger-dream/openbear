import time
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


def _full_plan(web_tests):
    plan = check.Plan(first=False, changed=set(), python=True, full=True)
    plan.web = {t: '--full' for t in web_tests}
    plan.go = {m: ['./...'] for m in check.GO_MODULES}
    return plan


def test_a_full_round_continues_where_the_last_run_stopped_and_ends_with_the_whole_result():
    current, state = {'app/a.py': '1', 'web/src/a.test.mjs': '2'}, {}
    tests = ['web/src/a.test.mjs', 'web/src/b.test.mjs', 'web/src/c.test.mjs']

    # First run: Python and Go finish, the frontend stops after one file with a judged failure.
    plan = _full_plan(tests)
    round_, carried = check.resume_full(plan, state, current)
    assert not carried and plan.python and len(plan.web) == 3
    py = check.Lane('python', passed=6700, skipped=15, total=6715, failures=[check.Failure('py', 't::x', 't::x', 'boom', 'tests/t.py', 'flaky')])
    web = check.Lane('web', complete=False, left=2, passed=3, units={'web/src/a.test.mjs'})
    lanes = {'python': py, 'go:node': check.Lane('go:node', passed=1083), 'go:gateway': check.Lane('go:gateway', passed=94), 'web': web}
    check.keep_round(state, round_, lanes)
    assert state[check.FULL_ROUND]['content'] == check.content_key(current)

    # Second run on the same content: only the two frontend files left.
    plan = _full_plan(tests)
    round_, carried = check.resume_full(plan, state, current)
    assert not plan.python and plan.go == {'node': [], 'gateway': []}
    assert sorted(plan.web) == ['web/src/b.test.mjs', 'web/src/c.test.mjs'] and plan.full_done == 1
    assert carried['python'].failures[0].verdict == 'flaky' and carried['python'].total == 6715
    now = check.Lane('web', passed=5, units={'web/src/b.test.mjs', 'web/src/c.test.mjs'})
    merged = check.merge_web(carried['web'], now)
    assert (merged.passed, merged.complete, merged.left, len(merged.units)) == (8, True, 0, 3)
    check.keep_round(state, round_, {**carried, 'web': merged})
    assert check.FULL_ROUND not in state

    # The next --full starts a new round.
    plan = _full_plan(tests)
    assert not check.resume_full(plan, state, current)[1] and plan.python and len(plan.web) == 3


def test_a_full_round_is_dropped_when_the_files_change():
    state, current = {}, {'app/a.py': '1'}
    round_, _ = check.resume_full(_full_plan([]), state, current)
    check.keep_round(state, round_, {'python': check.Lane('python', passed=1), 'web': check.Lane('web', complete=False, left=1)})
    plan = _full_plan(['web/src/a.test.mjs'])
    _, carried = check.resume_full(plan, state, {'app/a.py': '2'})
    assert not carried and plan.python and plan.web


def test_failures_judged_by_an_earlier_run_are_not_run_again(monkeypatch, tmp_path):
    monkeypatch.setattr(check, 'run', lambda *a, **k: pytest.fail('ran a judged failure again'))
    monkeypatch.setattr(check, 'run_web', lambda *a, **k: pytest.fail('ran a judged failure again'))
    lane = check.Lane('web', failures=[check.Failure('web', 'a › x', 'a › x', '', 'web/src/a.test.mjs', 'flaky'),
                                       check.Failure('web', 'b › y', 'b › y', '', 'web/src/b.test.mjs', 'old')])
    assert check.rerun_alone([lane], tmp_path, time.monotonic() + 60) == ''
    assert check.compare_with_head([lane], 'HEAD', tmp_path, time.monotonic() + 60) == ''
