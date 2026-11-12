#!/bin/bash
# Run inside the explicitly selected baseline, never borrow another Python ABI.
set -euo pipefail
expected="$1"
python -c 'import sys; assert ".".join(map(str, sys.version_info[:2])) == sys.argv[1], sys.version' "$expected"
base_python=$(python -c 'import sys; print(sys._base_executable)')
bash --version
git --version
uv --version
rm -rf /opt/testenv
mkdir -p /opt/testenv
cp /inputs/source/pyproject.toml /inputs/source/uv.lock /opt/testenv/
cd /opt/testenv
UV_PROJECT_ENVIRONMENT=/opt/testenv/.venv uv sync --frozen --extra dev --python "$base_python"
uv pip install --python /opt/testenv/.venv/bin/python pytest-xdist==3.8.0 execnet==2.1.2 pytest-timeout==2.4.0
/opt/testenv/.venv/bin/python -c 'import sys, importlib.metadata as m; assert ".".join(map(str, sys.version_info[:2])) == sys.argv[1]; assert m.version("pytest-xdist") == "3.8.0"; assert m.version("execnet") == "2.1.2"; assert m.version("pytest-timeout") == "2.4.0"; print(sys.version)' "$expected"
