"""Keep the project's no-browser-test rule effective in CI and local commands."""
from pathlib import Path
import json
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_release_uses_node_api_tests_and_http_package_smoke_without_a_browser():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["release"]["steps"]
    source = json.dumps(steps).lower()
    for forbidden in ("setup-chrome", "chrome_bin", "--chrome", "playwright", "puppeteer", "selenium"):
        assert forbidden not in source, forbidden
    node = next(step for step in steps if step.get("uses", "").startswith("actions/setup-node@"))
    backend = next(step for step in steps if "pytest -q" in step.get("run", ""))
    frontend = next(step for step in steps if "npm test" in step.get("run", ""))
    smoke = next(step for step in steps if "scripts/smoke_release_login.py" in step.get("run", ""))
    assert steps.index(node) < steps.index(backend) < steps.index(frontend) < steps.index(smoke)
    assert "npm ci" in frontend["run"] and "npm run build" in frontend["run"]
    assert "scripts/release_validation.py validate-tree" in smoke["run"]
    assert "sha256sum --check SHA256SUMS" in smoke["run"]
    for step in (backend, frontend, smoke):
        assert not step.get("continue-on-error")
        assert "|| true" not in step["run"]


def test_browser_suites_launchers_and_dependencies_are_retired_not_silently_skipped():
    assert not list((ROOT / "web").glob("src/**/*.browser.test.*"))
    assert not (ROOT / "web/test-support/realBrowser.mjs").exists()
    package = json.loads((ROOT / "web/package.json").read_text())
    assert package["scripts"]["test"] == "node --test"
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    assert not any(re.search(r"playwright|puppeteer|selenium", name, re.I) for name in dependencies)
    lock = json.loads((ROOT / "web/package-lock.json").read_text())
    assert not any(re.search(r"playwright|puppeteer|selenium", name, re.I) for name in lock["packages"])


def test_package_and_upload_smoke_use_direct_http_not_hidden_browser_subprocesses():
    package_smoke = (ROOT / "scripts/smoke_release_login.py").read_text()
    assert "TestClient(TestServer(" in package_smoke
    assert "probe_deployment" in package_smoke and "ws_connect" in package_smoke
    for forbidden in ("class CDP", "Runtime.evaluate", "remote-debugging-port", "create_subprocess_exec"):
        assert forbidden not in package_smoke
    upload_smoke = (ROOT / "web/test-support/httpUploadSmoke.mjs").read_text()
    assert 'from "../src/uploads.js"' in upload_smoke
    assert "await fetch(" in upload_smoke and "uploadFilesViaHttp(api," in upload_smoke
    assert not re.search(r"playwright|puppeteer|chromium|browserExecutable", upload_smoke, re.I)


def test_project_instructions_record_the_no_browser_test_rule():
    instructions = (ROOT / "AGENTS.md").read_text()
    assert "本项目禁止浏览器测试" in instructions
    assert "HTTP/WebSocket" in instructions and "2026-09-10" in instructions
