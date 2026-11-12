from __future__ import annotations

import copy
import io
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from release_support.common import ReleaseError, State, load, sha
from release_support import publication as publication_module
from release_support.publication import delete_candidates, publish, push_candidate
from release_support.github import GitHub


@pytest.mark.parametrize('path', ['', '/releases/latest'])
def test_github_repository_relative_paths_include_empty_root(monkeypatch, path):
    github = GitHub('fixture/openbear', 'only-in-memory-fixture')
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 150
        response = io.BytesIO(b'{"permissions":{"push":true}}')
        response.status = 200
        return response

    monkeypatch.setattr(github.opener, 'open', open_request)
    assert github.api(path) == {'permissions': {'push': True}}
    assert len(requests) == 1
    assert requests[0].full_url == 'https://api.github.com/repos/fixture/openbear' + path
    assert requests[0].get_header('Authorization') == 'Bearer only-in-memory-fixture'


class FakeGitHub:
    token = 'only-in-memory-fixture'

    def __init__(self, lose=None):
        self.main = 'base'
        self.tag = None
        self.release = None
        self.uploads = []
        self.payloads = {}
        self.lose = lose
        self.lost = False
        self.create_calls = 0
        self.publish_calls = 0
        self.branches = set()

    def maybe_lose(self, phase):
        if phase == self.lose and not self.lost:
            self.lost = True
            raise ReleaseError('response lost after ' + phase)

    def api(self, path, method='GET', data=None, missing=False):
        if path == '/git/ref/heads/main':
            return {'object': {'sha': self.main}}
        if path.startswith('/git/ref/heads/'):
            return {'object': {'sha': 'x'}} if path.removeprefix('/git/ref/heads/') in self.branches else None
        if path.startswith('/git/refs/heads/') and method == 'DELETE':
            self.branches.remove(path.removeprefix('/git/refs/heads/'))
            return None
        if path.startswith('/git/ref/tags/'):
            return {'object': {'sha': self.tag}} if self.tag else None
        if path.startswith('/releases/tags/'):
            return copy.deepcopy(self.release)
        if path == '/releases' and method == 'POST':
            self.create_calls += 1
            assert self.release is None
            self.release = {**data, 'id': 12, 'assets': [], 'upload_url': 'https://uploads.github.com/upload{?name}',
                            'html_url': 'https://github.com/fixture/openbear/releases/tag/v1.0.1'}
            self.maybe_lose('create')
            return copy.deepcopy(self.release)
        if path == '/releases/12' and method == 'PATCH':
            self.publish_calls += 1
            self.release.update(data)
            self.maybe_lose('publish')
            return copy.deepcopy(self.release)
        if path == '/releases/latest':
            return copy.deepcopy(self.release)
        raise AssertionError((path, method))

    def request(self, url, method='GET', data=None, **kwargs):
        if method == 'POST':
            name = parse_qs(urlsplit(url).query)['name'][0]
            assert name not in self.uploads, 'resuming must not reupload a confirmed asset'
            self.uploads.append(name)
            self.payloads[name] = data
            asset = {'id': 100 + len(self.uploads), 'name': name, 'state': 'uploaded', 'size': len(data),
                     'browser_download_url': 'https://github.com/fixture/openbear/releases/download/v1.0.1/' + name}
            self.release['assets'].append(asset)
            self.maybe_lose('upload')
            return asset
        assert method == 'HEAD' and kwargs['authenticated'] is False
        return 200

    def download_asset(self, asset):
        return self.payloads[asset['name']]


class LaggingGitHub(FakeGitHub):
    """GitHub's REST ref view trails the push: stale reads before the new value appears."""

    def __init__(self, stale_reads):
        super().__init__()
        self.stale_reads = stale_reads
        self.stale = {}

    def api(self, path, method='GET', data=None, missing=False):
        if path == '/git/ref/heads/main' and self.main != 'base' and self.stale.setdefault(path, self.stale_reads) > 0:
            self.stale[path] -= 1
            return {'object': {'sha': 'base'}}
        if path.startswith('/git/ref/tags/') and self.tag and self.stale.setdefault(path, self.stale_reads) > 0:
            self.stale[path] -= 1
            return None
        return super().api(path, method=method, data=data, missing=missing)


class FakeGit:
    def __init__(self, github, notes):
        self.github, self.notes = github, notes
        self.local_tag = False
        self.pushes = []
        self.refuse = None

    def __call__(self, args, _label, **kwargs):
        if 'status' in args:
            return ''
        if args[-2:] == ['rev-parse', 'HEAD'] or args[-1].endswith('^{commit}'):
            return 'candidate'
        if 'tag' in args and '--list' in args:
            return 'v1.0.1' if self.local_tag else ''
        if 'tag' in args and '-a' in args:
            assert '--cleanup=verbatim' in args
            self.local_tag = True
            return ''
        if 'cat-file' in args:
            return 'object candidate\ntagger OpenBear Release <release@openbear.invalid> 1 +0000\n\n' + self.notes.rstrip('\n')
        if 'rev-parse' in args:
            return 'tag-object'
        if 'push' in args:
            assert kwargs['env'].get('GIT_CONFIG_COUNT') == '2'
            self.pushes.append(args[-1])
            if '/release-candidate/' in args[-1]:
                if self.refuse:
                    error = ReleaseError('candidate-push: exit 1; log: x')
                    error.output = self.refuse
                    raise error
                self.github.branches.add(args[-1].split(':refs/heads/', 1)[1])
            elif args[-1] == 'candidate:refs/heads/main':
                self.github.main = 'candidate'
            else:
                self.github.tag = 'tag-object'
            return ''
        raise AssertionError(args)


@pytest.fixture
def publication(tmp_path):
    (tmp_path / 'source').mkdir()
    (tmp_path / 'assets').mkdir()
    notes = 'Version 1.0.0 → 1.0.1\n\n### Fixes\n\n- Original Markdown.\n'
    (tmp_path / 'notes.md').write_text(notes)
    assets = {}
    for name in ('install.sh', 'openbear-1.0.1.zip', 'SHA256SUMS', 'release-meta.json'):
        content = name.encode()
        (tmp_path / 'assets' / name).write_bytes(content)
        assets[name] = {'bytes': len(content), 'sha256': sha(content)}
    state = State(tmp_path, {'runId': '1.0.1-0123456789ab', 'candidate': {'localCommit': 'local', 'publicCommit': 'candidate'}, 'version': '1.0.1',
                            'publicBase': 'base', 'notes': notes, 'stages': {'package': {'result': {'assets': assets}}}})
    return tmp_path, state, notes


@pytest.mark.parametrize('phase', ['create', 'upload', 'publish'])
def test_unknown_remote_result_reconciles_before_retry(publication, phase):
    directory, state, notes = publication
    github = FakeGitHub(lose=phase)
    git = FakeGit(github, notes)
    with pytest.raises(ReleaseError, match='response lost'):
        publish(directory, state, git, github)
    restored = State(directory, load(directory / 'state.json'))
    result = publish(directory, restored, git, github)
    assert result['ok'] and result['latest']
    assert github.create_calls == 1 and github.publish_calls == 1
    assert len(github.uploads) == 4 and len(set(github.uploads)) == 4
    assert git.pushes == ['candidate:refs/heads/main', 'refs/tags/v1.0.1']
    assert github.release['body'] == notes
    assert github.token not in (directory / 'state.json').read_text()


def test_changed_zip_cannot_be_uploaded(publication):
    directory, state, notes = publication
    (directory / 'assets/openbear-1.0.1.zip').write_bytes(b'changed')
    github = FakeGitHub()
    with pytest.raises(ReleaseError, match='asset bytes differ'):
        publish(directory, state, FakeGit(github, notes), github)
    assert github.main == 'base' and not github.create_calls


def test_foreign_main_refuses_to_force_push(publication):
    directory, state, notes = publication
    github = FakeGitHub()
    github.main = 'someone-elses-commit'
    with pytest.raises(ReleaseError, match='Public main moved'):
        publish(directory, state, FakeGit(github, notes), github)
    assert not github.create_calls


def test_foreign_asset_is_not_deleted_or_replaced(publication):
    directory, state, notes = publication
    github = FakeGitHub(lose='upload')
    git = FakeGit(github, notes)
    with pytest.raises(ReleaseError):
        publish(directory, state, git, github)
    github.release['assets'].append({'name': 'foreign.txt', 'id': 444})
    with pytest.raises(ReleaseError, match='foreign or duplicate'):
        publish(directory, state, git, github)
    assert not github.publish_calls


def test_candidate_branch_is_pushed_first_and_replaced_candidates_are_removed(publication):
    directory, state, notes = publication
    github = FakeGitHub()
    git = FakeGit(github, notes)
    first = push_candidate(directory, state, git, github)
    assert first['branch'] == 'release-candidate/1.0.1-0123456789ab-candidate'
    assert git.pushes == ['candidate:refs/heads/release-candidate/1.0.1-0123456789ab-candidate']
    assert github.branches == {first['branch']} and github.main == 'base'
    state.value['candidate']['publicCommit'] = 'fixedcandidate0'
    second = push_candidate(directory, state, git, github)
    assert second['branch'] == 'release-candidate/1.0.1-0123456789ab-fixedcandida'
    assert github.branches == {second['branch']}
    assert load(directory / 'state.json')['candidateBranches'] == [second['branch']]
    assert delete_candidates(state, github) == [second['branch']]
    assert github.branches == set() and state.value['candidateBranches'] == []


def test_refused_candidate_push_pauses_with_githubs_reason(publication):
    directory, state, notes = publication
    github = FakeGitHub()
    git = FakeGit(github, notes)
    git.refuse = ('remote: error: GH013: Repository rule violations found for refs/heads/release-candidate/x.\n'
                  'remote: - Push cannot contain secrets\n'
                  'remote:   path: tests/ddns/test_provider_sign.py:12\n'
                  ' ! [remote rejected] candidate -> release-candidate/x (push declined due to repository rule violations)\n')
    with pytest.raises(ReleaseError) as refused:
        push_candidate(directory, state, git, github)
    assert refused.value.code == 2
    assert 'Push cannot contain secrets' in str(refused.value) and 'tests/ddns/test_provider_sign.py:12' in str(refused.value)
    assert github.main == 'base' and github.branches == set()


def test_only_this_runs_candidate_branches_are_deleted(publication):
    directory, state, notes = publication
    github = FakeGitHub()
    github.branches = {'release-candidate/other-run-candidate'}
    state.value['candidateBranches'] = ['release-candidate/other-run-candidate']
    with pytest.raises(ReleaseError, match='did not create'):
        delete_candidates(state, github)
    assert github.branches == {'release-candidate/other-run-candidate'}


def test_pushes_are_confirmed_once_githubs_ref_view_catches_up(publication, monkeypatch):
    monkeypatch.setattr(publication_module, 'CONFIRM_INTERVAL', 0)
    directory, state, notes = publication
    github = LaggingGitHub(stale_reads=3)
    result = publish(directory, state, FakeGit(github, notes), github)
    assert result['ok'] and github.main == 'candidate'
    assert github.stale == {'/git/ref/heads/main': 0, '/git/ref/tags/v1.0.1': 0}


def test_a_push_that_never_shows_up_still_pauses(publication, monkeypatch):
    monkeypatch.setattr(publication_module, 'CONFIRM_SECONDS', 0)
    directory, state, notes = publication
    github = LaggingGitHub(stale_reads=10**6)
    with pytest.raises(ReleaseError, match='Cannot confirm public-source push') as paused:
        publish(directory, state, FakeGit(github, notes), github)
    assert paused.value.code == 2 and not github.create_calls
