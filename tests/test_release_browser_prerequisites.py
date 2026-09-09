from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_release_provisions_chrome_before_frontend_tests_and_reuses_it_for_smoke():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["release"]["steps"]
    chrome = [step for step in steps if step.get("uses", "").startswith("browser-actions/setup-chrome@")]
    assert len(chrome) == 1, "Provision once, then reuse the exact executable"
    frontend = next(step for step in steps if "npm test" in step.get("run", ""))
    smoke = next(step for step in steps if "scripts/smoke_release_login.py" in step.get("run", ""))
    assert steps.index(chrome[0]) < steps.index(frontend) < steps.index(smoke)
    output = "${{ steps." + chrome[0]["id"] + ".outputs.chrome-path }}"
    assert frontend["env"]["CHROME_BIN"] == output
    assert smoke["env"]["CHROME"] == output
    assert "test -x \"$CHROME_BIN\"" in frontend["run"]
    assert "continue-on-error" not in frontend


def test_browser_suites_share_the_same_dependency_resolution():
    for name in (
        "web/src/views/consoleView/markdown.browser.test.mjs",
        "web/src/views/consoleView/processBorders.browser.test.mjs",
        "web/src/artifacts/artifactPreview.browser.test.mjs",
    ):
        source = (ROOT / name).read_text()
        assert "test-support/realBrowser.mjs" in source
        assert "browserExecutable()" in source
        assert "function browserExecutable(" not in source
        assert ".cache/ms-playwright" not in source
