from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "scripts" / "install.sh"
README_PATH = ROOT / "README.md"


def install_library(tmp_path: Path) -> Path:
    text = INSTALL_PATH.read_text(encoding="utf-8")
    assert text.rstrip().endswith('main "$@"')
    library = tmp_path / "install-library.sh"
    library.write_text(text.rsplit('main "$@"', 1)[0], encoding="utf-8")
    return library


def _render_model_menu(tmp_path: Path, payload: dict) -> str:
    library = install_library(tmp_path)
    # Use pytest's interpreter, not a potentially newer system python3.
    harness = '''set -euo pipefail
source "$1"
TEST_PYTHON="$2"
python3() { "$TEST_PYTHON" "$@"; }
print_model_menu "$3"
'''
    result = subprocess.run(
        [
            "bash", "-c", harness, "model-menu-test", str(library), sys.executable,
            json.dumps(payload, ensure_ascii=False),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    return result.stdout


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {
                "models": [
                    {"id": "model-a", "name": "模型 'A' \"别名\""},
                    {"id": "model-b", "name": "model-b"},
                    {"id": "model-c", "name": ""},
                    {"id": "model-d"},
                    {"id": "model-e", "name": None},
                ],
                "endpoint": "https://example.invalid/models",
            },
            "    1) model-a  (模型 'A' \"别名\")\n"
            "    2) model-b\n"
            "    3) model-c\n"
            "    4) model-d\n"
            "    5) model-e\n"
            "  共 5 个，来自 https://example.invalid/models\n",
            id="labels-and-optional-names",
        ),
        pytest.param({"models": []}, "  共 0 个，来自 ?\n", id="empty-models"),
        pytest.param(
            {"models": [{"id": "model-a"}], "endpoint": ""},
            "    1) model-a\n  共 1 个，来自 ?\n",
            id="endpoint-fallback",
        ),
    ],
)
def test_installer_model_menu_renders(tmp_path: Path, payload: dict, expected: str):
    assert _render_model_menu(tmp_path, payload) == expected


@pytest.mark.parametrize("count", [50, 51])
def test_installer_model_menu_display_limit(tmp_path: Path, count: int):
    payload = {
        "models": [{"id": f"model-{i}"} for i in range(1, count + 1)],
        "endpoint": "https://example.invalid/models",
    }
    expected = [f"  {i:>3}) model-{i}" for i in range(1, 51)]
    if count > 50:
        expected.append("  ... 还有 1 个未列出，请直接输入模型 ID")
    expected.append(f"  共 {count} 个，来自 https://example.invalid/models")
    assert _render_model_menu(tmp_path, payload).splitlines() == expected


def test_installer_bootstraps_python_before_configuration(tmp_path: Path):
    library = install_library(tmp_path)
    events = tmp_path / "events"
    harness = tmp_path / "bootstrap.sh"
    harness.write_text(
        f'''#!/bin/bash
set -euo pipefail
source {library}
PY_READY=0
curl() {{ [[ "${{1:-}}" == --version ]]; }}
python3() {{
    if [[ "$PY_READY" -eq 0 ]]; then return 127; fi
    command /usr/bin/python3 "$@"
}}
apt-get() {{
    echo "apt:$*" >> {events}
    if [[ "${{1:-}}" == install ]]; then PY_READY=1; fi
}}
print_banner() {{ :; }}
need_root() {{ :; }}
need_linux() {{ :; }}
collect_config() {{
    MODE=fresh
    python3 -c 'import json, urllib.request'
    echo collect-config >> {events}
}}
ensure_apt_packages() {{ echo full-deps >> {events}; }}
ensure_uv() {{ :; }}
sync_code() {{ :; }}
sync_python() {{ :; }}
build_web() {{ :; }}
write_runtime() {{ :; }}
write_service() {{ :; }}
ensure_firewall() {{ :; }}
start_and_verify() {{ :; }}
main
''',
        encoding="utf-8",
    )
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = events.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "apt:update -y"
    assert lines[1].startswith("apt:install -y --no-install-recommends python3")
    assert lines.index("collect-config") < lines.index("full-deps")
    assert "安装引导已就绪" in result.stdout


def test_installer_reports_bootstrap_failure_before_channel_probe(tmp_path: Path):
    library = install_library(tmp_path)
    marker = tmp_path / "collect-called"
    harness = tmp_path / "bootstrap-fails.sh"
    harness.write_text(
        f'''#!/bin/bash
set -euo pipefail
source {library}
curl() {{ [[ "${{1:-}}" == --version ]]; }}
python3() {{ return 127; }}
apt-get() {{ [[ "${{1:-}}" == update ]]; }}
print_banner() {{ :; }}
need_root() {{ :; }}
need_linux() {{ :; }}
collect_config() {{ touch {marker}; }}
main
''',
        encoding="utf-8",
    )
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert "安装引导依赖失败" in result.stderr
    assert "尚未开始渠道探测" in result.stderr
    assert "渠道不可用" not in result.stderr
    assert not marker.exists()


def test_release_dist_skips_node_but_missing_dist_builds_after_node(tmp_path: Path):
    library = install_library(tmp_path)
    release = tmp_path / "release"
    (release / "web" / "dist").mkdir(parents=True)
    (release / "web" / "dist" / "index.html").write_text("ready", encoding="utf-8")
    release_events = tmp_path / "release-events"
    release_harness = tmp_path / "release-web.sh"
    release_harness.write_text(
        f'''#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={release}
ensure_node() {{ echo node >> {release_events}; return 99; }}
build_web
''',
        encoding="utf-8",
    )
    release_result = subprocess.run(
        ["bash", str(release_harness)], cwd=ROOT, capture_output=True, text=True
    )
    assert release_result.returncode == 0, release_result.stdout + release_result.stderr
    assert not release_events.exists()
    assert "跳过 Node 检查" in release_result.stdout

    source = tmp_path / "source"
    (source / "web").mkdir(parents=True)
    (source / "web" / "package-lock.json").write_text("{}", encoding="utf-8")
    source_events = tmp_path / "source-events"
    source_harness = tmp_path / "source-web.sh"
    source_harness.write_text(
        f'''#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={source}
ensure_node() {{ echo node >> {source_events}; }}
npm() {{
    echo "npm:$*" >> {source_events}
    if [[ "${{1:-}} ${{2:-}}" == "run build" ]]; then
        mkdir -p "$INSTALL_DIR/web/dist"
        echo built > "$INSTALL_DIR/web/dist/index.html"
    fi
}}
build_web
''',
        encoding="utf-8",
    )
    source_result = subprocess.run(
        ["bash", str(source_harness)], cwd=ROOT, capture_output=True, text=True
    )
    assert source_result.returncode == 0, source_result.stdout + source_result.stderr
    assert source_events.read_text(encoding="utf-8").splitlines() == [
        "node",
        "npm:ci",
        "npm:run build",
    ]
    assert (source / "web" / "dist" / "index.html").is_file()


def test_installer_frontend_only_classification_skips_database_backup(tmp_path: Path):
    library = install_library(tmp_path)
    current = tmp_path / "current"
    incoming = tmp_path / "incoming"
    (current / "app").mkdir(parents=True)
    (current / "scripts").mkdir()
    (current / "web" / "dist").mkdir(parents=True)
    (current / "app" / "main.py").write_text("same", encoding="utf-8")
    (current / "web" / "dist" / "index.html").write_text("old", encoding="utf-8")
    shutil.copy2(INSTALL_PATH.parent / "updater.py", current / "scripts" / "updater.py")
    shutil.copytree(current, incoming, dirs_exist_ok=True)
    (incoming / "web" / "dist" / "index.html").write_text("new", encoding="utf-8")
    result_file = tmp_path / "result"
    harness = tmp_path / "classify.sh"
    harness.write_text(
        f'''#!/bin/bash
set -euo pipefail
source {library}
INSTALL_DIR={current}
classify_release_database_backup_need {incoming}
echo "$RELEASE_REQUIRES_DB_BACKUP" >> {result_file}
echo changed > {incoming}/app/main.py
classify_release_database_backup_need {incoming}
echo "$RELEASE_REQUIRES_DB_BACKUP" >> {result_file}
''',
        encoding="utf-8",
    )
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result_file.read_text(encoding="utf-8").splitlines() == ["0", "1"]


def _latest_download_blocks() -> list[str]:
    readme = README_PATH.read_text(encoding="utf-8")
    return [
        block
        for block in re.findall(r"```bash\n(.*?)```", readme, flags=re.S)
        if "releases/latest/download/install.sh" in block
    ]


def test_all_formal_download_examples_wait_for_complete_download_and_cleanup(tmp_path: Path):
    readme = README_PATH.read_text(encoding="utf-8")
    installer = INSTALL_PATH.read_text(encoding="utf-8")
    assert "<(curl" not in readme
    assert "<(curl" not in installer
    assert "curl -fSL --retry 3 -o" in readme
    assert "curl -fSL --retry 3 -o" in installer[:1200]
    blocks = _latest_download_blocks()
    assert len(blocks) == 3

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/bash\nexit 42\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    for index, block in enumerate(blocks):
        temp = tmp_path / f"tmp-{index}"
        temp.mkdir()
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["TMPDIR"] = str(temp)
        result = subprocess.run(["bash", "-c", block], cwd=ROOT, env=env, capture_output=True, text=True)
        assert result.returncode == 42, result.stdout + result.stderr
        assert list(temp.iterdir()) == []


def test_skip_firewall_download_example_preserves_environment(tmp_path: Path):
    block = _latest_download_blocks()[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "installer-environment"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        f'''#!/bin/bash
set -euo pipefail
out=""
while [[ "$#" -gt 0 ]]; do
    if [[ "$1" == -o ]]; then out="$2"; shift 2; continue; fi
    shift
done
cat > "$out" <<'PAYLOAD'
#!/bin/bash
printf '%s' "${{OPENBEAR_SKIP_FIREWALL:-}}" > {marker}
PAYLOAD
''',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["TMPDIR"] = str(tmp_path / "tmp")
    Path(env["TMPDIR"]).mkdir()
    result = subprocess.run(["bash", "-c", block], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == "1"
    assert list(Path(env["TMPDIR"]).iterdir()) == []


def test_readme_v012_bridge_discloses_required_boundaries():
    text = README_PATH.read_text(encoding="utf-8")
    assert "公开 v0.1.2 用户首次升级到 v0.2.0" in text
    assert "不要" in text and "/opt/openbear/scripts/install.sh" in text
    assert "不能反向保护" in text
    assert "结束正在运行的主控/Agent 任务和待回答交互" in text
    assert "手动刷新" in text
    assert "不会覆盖已有的激活模板或用户自定义模板" in text
    assert "Telegram 待答交互通知默认开启" in text
    assert "数据库未自动回退" not in text  # prose uses the clearer code/DB rollback wording below
    assert "代码/依赖自动回滚**不会**自动用旧快照覆盖数据库" in text
    assert "备份时间点之后的数据会丢失" in text
