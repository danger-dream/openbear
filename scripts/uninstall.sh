#!/usr/bin/env bash
# 卸载 OpenBear systemd 服务。默认不删部署目录里的配置和数据。

set -euo pipefail

SERVICE_NAME="${OPENBEAR_SERVICE:-openbear.service}"
DEFAULT_DIR="/opt/openbear"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "请用 root 运行" >&2
    exit 1
fi

read_tty() {
    local __var="$1" __prompt="$2" __default="${3:-}" __input=""
    if [[ -n "$__default" ]]; then
        __prompt="$__prompt [$__default]: "
    else
        __prompt="$__prompt: "
    fi
    if [[ -r /dev/tty ]]; then
        printf "%s" "$__prompt" > /dev/tty
        IFS= read -r __input < /dev/tty || __input=""
    else
        printf "%s" "$__prompt"
        IFS= read -r __input || __input=""
    fi
    [[ -z "$__input" && -n "$__default" ]] && __input="$__default"
    printf -v "$__var" "%s" "$__input"
}

INSTALL_DIR="${OPENBEAR_DIR:-}"
if [[ -z "$INSTALL_DIR" ]]; then
    read_tty INSTALL_DIR "部署目录" "$DEFAULT_DIR"
fi
INSTALL_DIR="${INSTALL_DIR%/}"

if systemctl list-unit-files "$SERVICE_NAME" >/dev/null 2>&1; then
    systemctl stop "$SERVICE_NAME" || true
    systemctl disable "$SERVICE_NAME" || true
fi
rm -f "/etc/systemd/system/${SERVICE_NAME}"
systemctl daemon-reload
echo "已停止并移除 $SERVICE_NAME"

if [[ "${OPENBEAR_PURGE:-}" == "1" ]]; then
    rm -rf "$INSTALL_DIR"
    echo "已删除 $INSTALL_DIR"
    exit 0
fi

echo "数据仍保留在 $INSTALL_DIR（含 openbear.json / data / workspace）"
echo "若要连目录一起删：OPENBEAR_PURGE=1 bash $0"
