"""Static contract/resource regression; actual Docker COPY evidence is a separate probe.

Do not emulate Docker's parent-pattern semantics with a home-grown matcher.
"""
import ast
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def validator():
    spec = importlib.util.spec_from_file_location('docker_resource_validator', ROOT / 'scripts/release_validation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prompt_inputs():
    names = ['prompts/guides/cron.md', 'prompts/guides/webhook.md', 'prompts/collab-member.md',
             'prompts/openbear-system.tpl', 'prompts/openbear-agent.tpl', 'prompts/personas/manifest.json']
    manifest = json.loads((ROOT / 'prompts/personas/manifest.json').read_text())
    for item in manifest['personas']:
        name = 'prompts/personas/' + item['id'] + '.tpl'
        names.append(name)
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == item['sha256']
    return names


def test_docker_prompt_contract_is_file_scoped_not_parent_directory_scoped():
    rules = [line.strip() for line in (ROOT / '.dockerignore').read_text().splitlines()
             if line.startswith(('!prompts/', 'prompts/'))]
    assert rules == [
        '!prompts/*.tpl', '!prompts/guides/*.md', '!prompts/collab-member.md',
        '!prompts/personas/manifest.json', '!prompts/personas/*.tpl',
        'prompts/*.tpl/*', 'prompts/guides/*/*', 'prompts/personas/*/*',
    ]
    # Directory exceptions include descendants. Reintroducing !prompts/ would
    # send private local files to the builder even with the narrow rules above.
    assert '!prompts/' not in rules
    assert 'COPY prompts/ ./prompts/' in (ROOT / 'Dockerfile').read_text()
    assert all((ROOT / name).is_file() for name in prompt_inputs())


def test_contract_inputs_satisfy_actual_eager_tool_resource_reads(tmp_path):
    for name in prompt_inputs():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    for tool in ('cron', 'webhook'):
        source = ROOT / 'app/tools' / (tool + '.py')
        tree = ast.parse(source.read_text())
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'GUIDE' for t in n.targets))
        read = ast.Expression(assignment.value.args[0])
        actual = eval(compile(read, str(source), 'eval'), {'Path': Path, '__file__': str(tmp_path / 'app/tools' / source.name)})
        assert actual == (ROOT / 'prompts/guides' / (tool + '.md')).read_text()
    assert (tmp_path / 'prompts/collab-member.md').read_bytes() == (ROOT / 'prompts/collab-member.md').read_bytes()


@pytest.mark.parametrize('version', ['0.16.1-dev.1', '0.16.1-beta.1', '0.16.1-rc.1', '0.16.1', '0.17.0'])
@pytest.mark.parametrize('damage', ['directory', 'manifest', 'template', 'digest', 'guide', 'entry'])
def test_candidate_resource_closure_rejects_missing_or_corrupt(tmp_path, version, damage):
    shutil.copytree(ROOT / 'prompts', tmp_path / 'prompts')
    if damage == 'directory':
        shutil.rmtree(tmp_path / 'prompts/personas')
    elif damage == 'manifest':
        (tmp_path / 'prompts/personas/manifest.json').unlink()
    elif damage == 'template':
        (tmp_path / 'prompts/personas/little-code.tpl').unlink()
    elif damage == 'digest':
        (tmp_path / 'prompts/personas/little-code.tpl').write_text('corrupt')
    elif damage == 'guide':
        (tmp_path / 'prompts/guides/cron.md').unlink()
    else:
        path = tmp_path / 'prompts/personas/manifest.json'
        data = json.loads(path.read_text())
        data['personas'].pop()
        path.write_text(json.dumps(data))
    module = validator()
    with pytest.raises(module.ReleaseValidationError):
        module.validate_prompt_resources(tmp_path, version)
    module.validate_prompt_resources(tmp_path, '0.16.0')
