#!/usr/bin/env bash
# OpenBear 一键安装 / 升级（Debian / Ubuntu 及同系，systemd 源码部署）
#
# 远程安装（先完整下载并校验 HTTP 成功，再执行）：
#   bash -c '
#     installer="$(mktemp)" || exit 1
#     trap "rm -f \"$installer\"" EXIT
#     curl -fSL --retry 3 -o "$installer" \
#       https://github.com/danger-dream/openbear/releases/latest/download/install.sh || exit $?
#     bash "$installer"
#   '
#
# 开发机也可以用 main 上的脚本；若还没有正式发行版，会回退到 git clone。
#
# 非交互（测试 / 自动化）：
#   OPENBEAR_NONINTERACTIVE=1 OPENBEAR_DIR=/opt/openbear \
#   OPENBEAR_BOT_TOKEN=... OPENBEAR_ADMIN_ID=... \
#   OPENBEAR_CHANNEL_NAME=default \
#   OPENBEAR_MODEL_BASE_URL=... OPENBEAR_MODEL_API_KEY=... \
#   OPENBEAR_MODEL_PROTOCOL=responses OPENBEAR_MODEL_ID=... \
#   bash scripts/install.sh
#
# 升级：对已有安装目录再跑一遍。默认 Upgrade：拉最新发行包、装依赖、必要时构建前端、重启。
# 不改 openbear.json / data / workspace / skills。
# 强制走 git：OPENBEAR_SOURCE=git

set -euo pipefail

REPO_SLUG="${OPENBEAR_REPO:-danger-dream/openbear}"
REPO_URL="${OPENBEAR_REPO_URL:-https://github.com/${REPO_SLUG}.git}"
REPO_REF="${OPENBEAR_REF:-main}"
SERVICE_NAME="${OPENBEAR_SERVICE:-openbear.service}"
SYSTEMD_UNIT_DIR="${OPENBEAR_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
DEFAULT_DIR="/opt/openbear"
DEFAULT_PORT="18961"
DEFAULT_NAME="老大"
NODE_VERSION="${OPENBEAR_NODE_VERSION:-v20.19.4}"
RELEASE_UPGRADE_PENDING=0
RELEASE_TXN=""
RELEASE_SERVICE_WAS_ACTIVE=0
RELEASE_SERVICE_WAS_ENABLED=0
RELEASE_REQUIRES_DB_BACKUP=1
RELEASE_DB_BACKUP_PATH=""
APT_UPDATED=0

if [[ -t 1 ]]; then
    C_RESET='\033[0m'; C_BOLD='\033[1m'
    C_RED='\033[31m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_BLUE='\033[36m'
else
    C_RESET=''; C_BOLD=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi

info()    { printf "${C_BLUE}[i]${C_RESET} %s\n" "$*"; }
ok()      { printf "${C_GREEN}[✓]${C_RESET} %s\n" "$*"; }
warn()    { printf "${C_YELLOW}[!]${C_RESET} %s\n" "$*"; }
err()     { printf "${C_RED}[✗]${C_RESET} %s\n" "$*" >&2; }
section() { printf "\n${C_BOLD}=== %s ===${C_RESET}\n" "$*"; }
die()     { err "$*"; exit 1; }

NONINTERACTIVE=0
[[ "${OPENBEAR_NONINTERACTIVE:-}" == "1" ]] && NONINTERACTIVE=1

read_tty() {
    local __var="$1" __prompt="$2" __default="${3:-}" __input=""
    if [[ -n "$__default" ]]; then
        __prompt="$__prompt [$__default]: "
    else
        __prompt="$__prompt: "
    fi
    if [[ "$NONINTERACTIVE" -eq 1 ]]; then
        __input="$__default"
        printf -v "$__var" "%s" "$__input"
        return 0
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

confirm_tty() {
    local prompt="$1" default="${2:-Y}" hint ans
    if [[ "$NONINTERACTIVE" -eq 1 ]]; then
        [[ "$default" == "Y" ]]
        return $?
    fi
    [[ "$default" == "Y" ]] && hint="[Y/n]" || hint="[y/N]"
    while true; do
        read_tty ans "$prompt $hint" ""
        [[ -z "$ans" ]] && ans="$default"
        case "$ans" in
            y|Y|yes|YES) return 0 ;;
            n|N|no|NO)   return 1 ;;
            *) warn "请输入 y / n" ;;
        esac
    done
}

run_channel_py() {
    local action="$1"
    _OB_ACTION="$action" \
    _OB_BASE="${MODEL_BASE_URL:-}" \
    _OB_KEY="${MODEL_API_KEY:-}" \
    _OB_PROTO="${MODEL_PROTOCOL:-}" \
    _OB_MID="${MODEL_ID:-}" \
    python3 - <<'PY'
import json, os, sys, time, urllib.error, urllib.request

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")

def fail(msg, **extra):
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    emit(payload)
    raise SystemExit(1)

action = os.environ.get("_OB_ACTION", "")
base = (os.environ.get("_OB_BASE") or "").strip().rstrip("/")
key = (os.environ.get("_OB_KEY") or "").strip()
proto = (os.environ.get("_OB_PROTO") or "responses").strip().lower()
mid = (os.environ.get("_OB_MID") or "").strip()
if not (base.startswith("http://") or base.startswith("https://")):
    fail("Base URL 必须以 http:// 或 https:// 开头")

def headers(json_body=False):
    h = {"Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    if key:
        h["Authorization"] = f"Bearer {key}"
        if proto == "anthropic":
            h["x-api-key"] = key
            h.setdefault("anthropic-version", "2023-06-01")
    return h

def request(method, url, body=None, timeout=45):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers(body is not None), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"

def parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None

def extract_models(payload):
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    models, seen = [], set()
    for item in rows:
        if isinstance(item, str):
            item_id, label = item.strip(), item.strip()
        elif isinstance(item, dict):
            item_id = str(item.get("id") or item.get("name") or "").strip()
            label = str(item.get("display_name") or item.get("displayName") or item.get("name") or item_id).strip() or item_id
        else:
            continue
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        models.append({"id": item_id, "name": label})
    return models

def list_models():
    endpoints = []
    if base.endswith("/v1"):
        endpoints.append(base[:-3].rstrip("/") + "/models")
    endpoints.append(base + "/models")
    endpoints = list(dict.fromkeys(endpoints))
    errors = []
    for ep in endpoints:
        code, raw = request("GET", ep, timeout=20)
        if code in {404, 405} and ep != endpoints[-1]:
            errors.append(f"{ep}: HTTP {code}")
            continue
        payload = parse_json(raw)
        if 200 <= code < 300 and payload is not None:
            models = extract_models(payload)
            if models is not None:
                emit({"ok": True, "models": models, "count": len(models), "endpoint": ep})
                return
            errors.append(f"{ep}: models_response_invalid")
        else:
            errors.append(f"{ep}: HTTP {code} {(raw or '')[:180]}")
    fail("拉取模型失败: " + " | ".join(errors)[-900:])

def extract_text(payload):
    if not isinstance(payload, dict):
        return ""
    if proto == "responses":
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return payload["output_text"].strip()
        bits = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    bits.append(part.get("text") or "")
        return " ".join(bits).strip()
    if proto == "chat":
        choices = payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            return str(msg.get("content") or "").strip()
        return ""
    if proto == "anthropic":
        bits = []
        for blk in payload.get("content") or []:
            if isinstance(blk, dict) and blk.get("type") == "text":
                bits.append(blk.get("text") or "")
        return " ".join(bits).strip()
    return ""

def payload_error(payload, raw):
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:400]
        if err:
            return str(err)[:400]
    return (raw or "")[:400]

def test_model():
    if not mid:
        fail("模型 ID 不能为空")
    if proto not in {"responses", "anthropic", "chat"}:
        fail(f"不支持的协议: {proto}")
    if proto == "responses":
        url = base + "/responses"
        body = {"model": mid, "input": [{"role": "user", "content": "请只回答 OK"}], "stream": False, "max_output_tokens": 128}
    elif proto == "chat":
        url = base + "/chat/completions"
        body = {"model": mid, "messages": [{"role": "user", "content": "请只回答 OK"}], "stream": False, "max_tokens": 64}
    else:
        url = base + "/messages"
        body = {"model": mid, "max_tokens": 64, "messages": [{"role": "user", "content": "请只回答 OK"}]}
    started = time.monotonic()
    code, raw = request("POST", url, body, timeout=90)
    elapsed = int((time.monotonic() - started) * 1000)
    payload = parse_json(raw)
    err = payload_error(payload, raw)
    if code < 200 or code >= 300:
        fail(f"HTTP {code}: {err}", elapsedMs=elapsed)
    text = extract_text(payload or {})
    if err and not text:
        fail(err, elapsedMs=elapsed)
    # 有些模型会把 16~32 token 全部留给内部预留，探测得到 200 但 output 为空。
    # HTTP 成功且没有 error，即可认为渠道和主模型可达。
    if not text:
        status = str((payload or {}).get("status") or "")
        incomplete = (payload or {}).get("incomplete_details") if isinstance(payload, dict) else None
        if status in {"incomplete", "in_progress"} or incomplete:
            text = "OK"
        else:
            fail("上游返回空回复", elapsedMs=elapsed)
    emit({"ok": True, "elapsedMs": elapsed, "snippet": text.replace("\n", " ")[:200], "status": code})

if action == "list":
    list_models()
elif action == "test":
    test_model()
else:
    fail(f"unknown action: {action}")
PY
}

print_model_menu() {
    python3 -c 'import json,sys
d=json.loads(sys.argv[1])
models=d.get("models") or []
limit=min(len(models), 50)
for i, m in enumerate(models[:limit], 1):
    extra = f"  ({m.get("name")})" if m.get("name") and m.get("name") != m.get("id") else ""
    print(f"  {i:>3}) {m.get("id","")}{extra}")
if len(models) > limit:
    print(f"  ... 还有 {len(models)-limit} 个未列出，请直接输入模型 ID")
print(f"  共 {len(models)} 个，来自 {d.get("endpoint") or "?"}")
' "$1"
}

pick_model_from_json() {
    local json="$1" choice="$2"
    MODEL_ID="$(python3 -c 'import json,sys
d=json.loads(sys.argv[1]); choice=(sys.argv[2] or "").strip(); models=d.get("models") or []
if choice.isdigit():
    idx=int(choice)
    if 1<=idx<=len(models):
        print(models[idx-1]["id"]); raise SystemExit
    raise SystemExit("bad-index")
for m in models:
    if m.get("id")==choice:
        print(choice); raise SystemExit
if choice:
    print(choice); raise SystemExit
raise SystemExit("empty")
' "$json" "$choice")" || return 1
    MODEL_NAME="$(python3 -c 'import json,sys
d=json.loads(sys.argv[1]); mid=sys.argv[2]
for m in d.get("models") or []:
    if m.get("id")==mid:
        print(m.get("name") or mid); raise SystemExit
print(mid)
' "$json" "$MODEL_ID")"
}

collect_channel() {
    local raw proto_choice list_json choice probe
    section "模型渠道（必填，安装前会测通）"
    echo "  填渠道名称、Base URL、API Key，再选择协议和主模型。"
    echo "  脚本会先拉模型列表，再对选中的主模型发一条测试请求。"
    echo

    while [[ -z "$MODEL_PROVIDER" ]]; do
        read_tty MODEL_PROVIDER "渠道名称（字母数字 . _ -）" "default"
    done
    [[ "$MODEL_PROVIDER" =~ ^[A-Za-z0-9_.-]{1,40}$ ]] || die "渠道名称不合法: $MODEL_PROVIDER"

    while [[ -z "$MODEL_BASE_URL" ]]; do
        read_tty MODEL_BASE_URL "渠道 Base URL（如 http://127.0.0.1:22122/v1）" ""
        [[ -z "$MODEL_BASE_URL" ]] && warn "Base URL 不能为空"
    done
    MODEL_BASE_URL="${MODEL_BASE_URL%/}"
    case "$MODEL_BASE_URL" in
        http://*|https://*) ;;
        *) die "Base URL 必须以 http:// 或 https:// 开头" ;;
    esac

    while [[ -z "$MODEL_API_KEY" ]]; do
        read_tty MODEL_API_KEY "渠道 API Key" ""
        [[ -z "$MODEL_API_KEY" ]] && warn "API Key 不能为空"
    done

    while true; do
        if [[ -z "$MODEL_PROTOCOL" ]]; then
            echo
            echo "  1) responses   OpenAI Responses（Parrot 等）"
            echo "  2) anthropic   Claude 原生 Messages"
            echo "  3) chat        OpenAI Chat Completions"
            read_tty proto_choice "选择协议" "1"
            case "$proto_choice" in
                1|responses|RESPONSES) MODEL_PROTOCOL="responses" ;;
                2|anthropic|ANTHROPIC) MODEL_PROTOCOL="anthropic" ;;
                3|chat|CHAT)           MODEL_PROTOCOL="chat" ;;
                *) warn "请输入 1 / 2 / 3"; continue ;;
            esac
        fi
        case "$MODEL_PROTOCOL" in
            responses|anthropic|chat) ;;
            *) die "不支持的协议: $MODEL_PROTOCOL" ;;
        esac

        info "正在从渠道拉取模型列表（$MODEL_PROTOCOL）..."
        if ! list_json="$(run_channel_py list)"; then
            warn "拉取模型失败: $(printf '%s' "$list_json" | python3 -c 'import json,sys; print((json.loads(sys.stdin.read()) or {}).get("error","未知错误"))' 2>/dev/null || echo 未知错误)"
            if [[ "$NONINTERACTIVE" -eq 1 ]]; then
                die "渠道不可用，已停止安装"
            fi
            MODEL_PROTOCOL=""
            confirm_tty "改协议或检查 URL/Key 后重试？" Y || die "已取消"
            continue
        fi

        local count
        count="$(python3 -c 'import json,sys; print(int((json.loads(sys.argv[1]) or {}).get("count") or 0))' "$list_json")"
        if [[ "$count" -gt 0 ]]; then
            ok "拉到 ${count} 个模型"
            print_model_menu "$list_json"
        else
            warn "渠道通了，但模型列表是空的，需要手填模型 ID"
        fi

        if [[ -z "$MODEL_ID" ]]; then
            if [[ "$NONINTERACTIVE" -eq 1 ]]; then
                die "非交互安装必须设置 OPENBEAR_MODEL_ID"
            fi
            if [[ "$count" -gt 0 ]]; then
                read_tty choice "选择主模型（序号或模型 ID）" "1"
            else
                read_tty choice "主模型 ID" ""
            fi
            if ! pick_model_from_json "$list_json" "$choice"; then
                warn "选择无效"
                continue
            fi
        elif [[ "$count" -gt 0 ]]; then
            pick_model_from_json "$list_json" "$MODEL_ID" || true
            [[ -n "$MODEL_NAME" ]] || MODEL_NAME="$MODEL_ID"
        fi
        [[ -n "$MODEL_ID" ]] || { warn "模型 ID 不能为空"; continue; }
        [[ -n "$MODEL_NAME" ]] || MODEL_NAME="$MODEL_ID"
        info "主模型: ${MODEL_PROVIDER}/${MODEL_ID}"

        info "正在测试渠道是否可用..."
        if probe="$(run_channel_py test)"; then
            python3 -c 'import json,sys
d=json.loads(sys.argv[1])
print("  耗时 %s ms  回复: %s" % (d.get("elapsedMs", 0), (d.get("snippet") or "")[:80]))
' "$probe"
            ok "渠道可用：${MODEL_PROVIDER}/${MODEL_ID}（${MODEL_PROTOCOL}）"
            return 0
        fi
        warn "测试失败: $(printf '%s' "$probe" | python3 -c 'import json,sys; print((json.loads(sys.stdin.read()) or {}).get("error","未知错误"))' 2>/dev/null || echo 未知错误)"
        if [[ "$NONINTERACTIVE" -eq 1 ]]; then
            die "主模型测试失败，已停止安装"
        fi
        MODEL_PROTOCOL=""
        MODEL_ID=""
        MODEL_NAME=""
        confirm_tty "换协议或换模型再测？" Y || die "已取消"
    done
}

detect_urls() {
    local port="$1" ip pub
    INTERNAL_URL=""
    EXTERNAL_URL=""
    for ip in $(hostname -I 2>/dev/null || true); do
        case "$ip" in
            127.*|::1) continue ;;
            10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|100.[6-9][0-9].*|100.1[0-2][0-9].*|100.13[0-9].*)
                INTERNAL_URL="http://${ip}:${port}"
                break
                ;;
        esac
    done
    [[ -n "$INTERNAL_URL" ]] || INTERNAL_URL="http://127.0.0.1:${port}"
    local src
    for src in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
        pub="$(curl -4 -fsS --max-time 4 "$src" 2>/dev/null | tr -d '[:space:]' || true)"
        if [[ "$pub" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            EXTERNAL_URL="http://${pub}:${port}"
            break
        fi
    done
}

read_web_secret() {
    local db="$INSTALL_DIR/data/openbear.db" i
    WEB_SECRET=""
    [[ -x "$INSTALL_DIR/.venv/bin/python" ]] || return 1
    for i in $(seq 1 15); do
        WEB_SECRET="$("$INSTALL_DIR/.venv/bin/python" - "$db" <<'PY'
import sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    raise SystemExit
con = sqlite3.connect(p)
row = con.execute("SELECT value FROM app_state WHERE key='web_secret_key'").fetchone()
print((row[0] if row and row[0] else "").strip())
PY
)"
        [[ -n "$WEB_SECRET" ]] && return 0
        sleep 1
    done
    return 1
}

print_banner() {
    cat <<'EOF'

  ╔═══════════════════════════════════════════════════════╗
  ║                   OpenBear  自部署                    ║
  ║     单人自用 Agent Web 控制台 · Linux systemd         ║
  ╚═══════════════════════════════════════════════════════╝

EOF
    printf "  仓库 : https://github.com/%s\n" "$REPO_SLUG"
    printf "  默认目录 : %s\n" "$DEFAULT_DIR"
    printf "  Web 端口 : %s\n\n" "$DEFAULT_PORT"
}

need_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        die "请用 root 运行（需要写 systemd 与安装依赖）"
    fi
}

need_linux() {
    [[ "$(uname -s)" == "Linux" ]] || die "仅支持 Linux"
    if ! command -v apt-get >/dev/null 2>&1; then
        die "当前只支持 Debian / Ubuntu 及使用 apt 的同系发行版"
    fi
}

node_major() {
    command -v node >/dev/null 2>&1 || { echo 0; return; }
    node -p 'parseInt(process.versions.node.split(".")[0], 10)' 2>/dev/null || echo 0
}

apt_update_once() {
    if [[ "$APT_UPDATED" -eq 0 ]]; then
        if ! apt-get update -y; then
            return 1
        fi
        APT_UPDATED=1
    fi
}

ensure_bootstrap_packages() {
    section "准备安装引导"
    export DEBIAN_FRONTEND=noninteractive
    local -a packages=()
    curl --version >/dev/null 2>&1 || packages+=(curl)
    python3 -c 'import json, urllib.request' >/dev/null 2>&1 || packages+=(python3)
    [[ -r /etc/ssl/certs/ca-certificates.crt ]] || packages+=(ca-certificates)
    if [[ "${#packages[@]}" -gt 0 ]]; then
        info "补齐安装引导依赖: ${packages[*]}"
        if ! apt_update_once; then
            die "更新 apt 索引失败，尚未开始渠道探测"
        fi
        if ! apt-get install -y --no-install-recommends "${packages[@]}"; then
            die "安装引导依赖失败（需要 curl、CA 证书和 python3），尚未开始渠道探测"
        fi
    fi
    curl --version >/dev/null 2>&1 || die "安装引导缺少 curl，尚未开始渠道探测"
    python3 -c 'import json, urllib.request' >/dev/null 2>&1 \
        || die "安装引导缺少可用的 python3，尚未开始渠道探测"
    [[ -r /etc/ssl/certs/ca-certificates.crt ]] \
        || die "安装引导缺少 CA 证书，尚未开始渠道探测"
    ok "安装引导已就绪: curl / CA / $(python3 -V 2>&1)"
}

ensure_apt_packages() {
    section "安装系统依赖"
    export DEBIAN_FRONTEND=noninteractive
    apt_update_once || die "更新 apt 索引失败"
    apt-get install -y --no-install-recommends \
        ca-certificates curl git tar xz-utils \
        python3 python3-venv python3-pip python3-dev \
        build-essential pkg-config
    ok "apt 依赖已就绪"
}

ensure_uv() {
    export PATH="${HOME}/.local/bin:/root/.local/bin:/usr/local/bin:${PATH}"
    if ! command -v uv >/dev/null 2>&1; then
        info "安装 uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="${HOME}/.local/bin:/root/.local/bin:/usr/local/bin:${PATH}"
        command -v uv >/dev/null 2>&1 || die "uv 安装失败"
    fi
    # systemd-run 默认 PATH 不含 ~/.local/bin，界面更新会找不到 uv。
    local uv_bin
    uv_bin="$(command -v uv)"
    if [[ -n "$uv_bin" && "$uv_bin" != "/usr/local/bin/uv" && -d /usr/local/bin && ! -e /usr/local/bin/uv ]]; then
        ln -sfn "$uv_bin" /usr/local/bin/uv || warn "无法把 uv 链到 /usr/local/bin，界面更新可能找不到 uv"
    fi
    ok "uv: $(uv --version)"
}

ensure_node() {
    local major narch ver
    major="$(node_major)"
    if [[ "$major" -ge 18 ]] && command -v npm >/dev/null 2>&1; then
        ok "Node: $(node -v) / npm $(npm -v 2>/dev/null || echo '?')"
        return 0
    fi
    if [[ "$major" -ge 18 ]]; then
        info "系统 Node 可用但缺少 npm，改装官方 Node 发行包"
    fi
    case "$(uname -m)" in
        x86_64) narch=x64 ;;
        aarch64|arm64) narch=arm64 ;;
        *) die "不支持的 CPU 架构: $(uname -m)，请先自行安装 Node 18+" ;;
    esac
    ver="$NODE_VERSION"
    info "安装 Node ${ver}..."
    curl -fsSL "https://nodejs.org/dist/${ver}/node-${ver}-linux-${narch}.tar.xz" \
        | tar -xJ -C /usr/local --strip-components=1
    command -v node >/dev/null 2>&1 || die "Node 安装失败"
    ok "Node: $(node -v)"
}

collect_config() {
    section "安装目录"
    if [[ "$NONINTERACTIVE" -eq 1 ]]; then
        INSTALL_DIR="${OPENBEAR_DIR:-$DEFAULT_DIR}"
    else
        read_tty INSTALL_DIR "部署目录" "$DEFAULT_DIR"
    fi
    INSTALL_DIR="${INSTALL_DIR%/}"
    [[ -n "$INSTALL_DIR" ]] || die "部署目录不能为空"
    case "$INSTALL_DIR" in
        /| /boot| /etc| /usr| /bin| /sbin) die "拒绝使用系统目录: $INSTALL_DIR" ;;
    esac

    MODE="${OPENBEAR_MODE:-}"
    if [[ -z "$MODE" ]]; then
        # 发行包安装没有 .git；有 openbear.json 就视为已有实例，默认升级。
        if [[ -f "$INSTALL_DIR/openbear.json" ]]; then
            MODE="upgrade"
        elif [[ -e "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]]; then
            MODE=""
        else
            MODE="fresh"
        fi
    fi

    if [[ -z "$MODE" ]]; then
        warn "目录已存在: $INSTALL_DIR"
        ls -la "$INSTALL_DIR" 2>/dev/null | sed 's/^/    /' | head -12 || true
        echo
        local choice
        read_tty choice "已存在，[U]pgrade 升级代码 / [O]verwrite 覆盖配置 / [C]ancel 取消" "U"
        case "${choice^^}" in
            U) MODE="upgrade" ;;
            O) MODE="overwrite" ;;
            *) info "已取消"; exit 0 ;;
        esac
    fi
    info "模式: $MODE"

    DISPLAY_NAME="${OPENBEAR_DISPLAY_NAME:-}"
    TG_TOKEN="${OPENBEAR_BOT_TOKEN:-}"
    TG_ADMIN="${OPENBEAR_ADMIN_ID:-}"
    MODEL_PROVIDER="${OPENBEAR_CHANNEL_NAME:-${OPENBEAR_MODEL_PROVIDER:-}}"
    MODEL_BASE_URL="${OPENBEAR_MODEL_BASE_URL:-}"
    MODEL_API_KEY="${OPENBEAR_MODEL_API_KEY:-}"
    MODEL_PROTOCOL="${OPENBEAR_MODEL_PROTOCOL:-}"
    MODEL_ID="${OPENBEAR_MODEL_ID:-}"
    MODEL_NAME="${OPENBEAR_MODEL_NAME:-}"
    WEB_PORT="${OPENBEAR_WEB_PORT:-}"

    if [[ "$MODE" == "upgrade" ]]; then
        info "升级模式：保留 openbear.json / data / workspace / skills"
        [[ -f "$INSTALL_DIR/openbear.json" ]] || die "升级失败：未找到 $INSTALL_DIR/openbear.json"
        return 0
    fi

    section "Telegram"
    echo "  Bot Token：用 Telegram 打开 @BotFather，创建机器人后复制 token"
    echo "  Admin Telegram 用户 ID：你自己的 Telegram 数字账号 ID"
    echo "    - 不是 Bot Token，也不是 @用户名"
    echo "    - 打开 @userinfobot 或 @getidsbot，把回你的 Id 填到下面"
    echo
    while [[ -z "${TG_TOKEN}" ]]; do
        read_tty TG_TOKEN "Telegram Bot Token" ""
        [[ -z "$TG_TOKEN" ]] && warn "Bot Token 不能为空"
    done
    while [[ -z "${TG_ADMIN}" || ! "$TG_ADMIN" =~ ^[0-9]+$ ]]; do
        read_tty TG_ADMIN "Admin 的 Telegram 用户数字 ID" ""
        [[ ! "$TG_ADMIN" =~ ^[0-9]+$ ]] && warn "必须是 Telegram 用户数字 ID，例如 123456789"
    done

    section "称呼"
    if [[ -z "$DISPLAY_NAME" ]]; then
        read_tty DISPLAY_NAME "Web 里怎么称呼你" "$DEFAULT_NAME"
    fi
    [[ -n "$DISPLAY_NAME" ]] || DISPLAY_NAME="$DEFAULT_NAME"

    collect_channel

    section "Web"
    if [[ -z "$WEB_PORT" ]]; then
        read_tty WEB_PORT "Web 端口" "$DEFAULT_PORT"
    fi
    [[ "$WEB_PORT" =~ ^[0-9]+$ ]] || die "端口必须是数字"
    if ss -tlnp 2>/dev/null | grep -qE ":${WEB_PORT}\\b"; then
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            info "端口 ${WEB_PORT} 当前由 ${SERVICE_NAME} 占用，安装完成后会重启接管"
        else
            warn "端口 ${WEB_PORT} 已被占用"
            ss -tlnp 2>/dev/null | grep ":${WEB_PORT}\\b" | sed 's/^/    /' || true
            confirm_tty "继续使用此端口？（启动可能失败）" N || die "已取消"
        fi
    fi
}

write_install_source() {
    local source="$1" version="${2:-}" tag="${3:-}"
    mkdir -p "$INSTALL_DIR/data"
    INSTALL_DIR="$INSTALL_DIR" REPO_REF="${REPO_REF:-}" \
    OPENBEAR_SRC="$source" OPENBEAR_SRC_VER="$version" OPENBEAR_SRC_TAG="$tag" python3 - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ["INSTALL_DIR"]) / "data" / "install-source.json"
payload = {
    "source": os.environ.get("OPENBEAR_SRC") or "",
    "version": os.environ.get("OPENBEAR_SRC_VER") or "",
    "tag": os.environ.get("OPENBEAR_SRC_TAG") or "",
    "ref": os.environ.get("REPO_REF") or "",
}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

fetch_latest_release_meta() {
    local api="https://api.github.com/repos/${REPO_SLUG}/releases/latest"
    local raw dest="$1" json_file
    raw="$(curl -fsSL -A "OpenBear-Install" "$api" || true)"
    [[ -n "$raw" ]] || return 1
    json_file="$(mktemp)"
    printf "%s" "$raw" > "$json_file"
    python3 - "$dest" "$json_file" <<'PY'
import json, sys
from pathlib import Path
dest = Path(sys.argv[1])
try:
    data = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
    raise SystemExit(1)
tag = str(data.get("tag_name") or "")
ver = tag[1:] if tag.startswith("v") else tag
zip_url = sums_url = ""
for asset in data.get("assets") or []:
    if not isinstance(asset, dict):
        continue
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    if name.endswith(".zip") and name.startswith("openbear-"):
        zip_url = url
    if name == "SHA256SUMS":
        sums_url = url
if not zip_url:
    raise SystemExit(1)
dest.write_text(
    f"RELEASE_TAG={tag}\nRELEASE_VERSION={ver}\nRELEASE_ZIP_URL={zip_url}\nRELEASE_SUMS_URL={sums_url}\n",
    encoding="utf-8",
)
PY
    local rc=$?
    rm -f "$json_file"
    return "$rc"
}

validate_release_package() {
    local pkg="$1" output
    if [[ ! -f "$pkg/scripts/release_validation.py" ]]; then
        err "发行包缺少 scripts/release_validation.py"
        return 1
    fi
    if ! output="$(python3 "$pkg/scripts/release_validation.py" validate-tree --root "$pkg" --version "$RELEASE_VERSION")"; then
        err "发行包校验失败: $output"
        return 1
    fi
    info "发行包校验: $output"
}

copy_release_tree() {
    local pkg="$1" f
    mkdir -p "$INSTALL_DIR/web"
    rm -rf "$INSTALL_DIR/app" "$INSTALL_DIR/web/dist" "$INSTALL_DIR/prompts" "$INSTALL_DIR/scripts"
    cp -a "$pkg/app" "$INSTALL_DIR/app" || return 1
    cp -a "$pkg/web/dist" "$INSTALL_DIR/web/dist" || return 1
    cp -a "$pkg/prompts" "$INSTALL_DIR/prompts" || return 1
    cp -a "$pkg/scripts" "$INSTALL_DIR/scripts" || return 1
    for f in pyproject.toml uv.lock openbear.service openbear.json.example release-meta.json; do
        cp -a "$pkg/$f" "$INSTALL_DIR/$f" || return 1
    done
    for f in README.md README; do
        if [[ -e "$pkg/$f" ]]; then
            cp -a "$pkg/$f" "$INSTALL_DIR/$f" || return 1
        else
            rm -f "$INSTALL_DIR/$f"
        fi
    done
}

apply_release_tree() {
    local pkg="$1"
    mkdir -p "$INSTALL_DIR"
    validate_release_package "$pkg" || return 1
    copy_release_tree "$pkg"
}

prepare_release_dependencies() {
    local stage="$1" py="3.12"
    section "准备新版本 Python 依赖"
    if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
        py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    fi
    rm -rf "$stage/.venv"
    # 非 editable 安装避免 .venv 搬到正式目录后仍引用 staging 源路径。
    UV_PROJECT_ENVIRONMENT="$stage/.venv" uv sync \
        --frozen --no-editable --python "$py" --directory "$stage" || return 1
    [[ -x "$stage/.venv/bin/python" ]] || { err "新版本依赖环境未生成"; return 1; }
    (
        cd "$stage"
        PYTHONPATH="$stage" "$stage/.venv/bin/python" -c \
            'import aiogram, aiohttp, aiosqlite, httpx, pydantic, yaml'
    ) || return 1
    # venv 会整体搬到正式 .venv；修正 console script/activate 中的绝对 staging 路径。
    _OB_STAGE_VENV="$stage/.venv" _OB_FINAL_VENV="$INSTALL_DIR/.venv" python3 - <<'PY' || return 1
import os
from pathlib import Path
root = Path(os.environ["_OB_STAGE_VENV"])
if not root.is_symlink():
    old = str(root).encode()
    new = os.environ["_OB_FINAL_VENV"].encode()
    for path in (root / "bin").iterdir():
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            continue
        data = path.read_bytes()
        if b"\0" not in data and old in data:
            path.write_bytes(data.replace(old, new))
PY
    ok "新版本依赖已在 staging 准备完成"
}

classify_release_database_backup_need() {
    local pkg="$1" classification
    RELEASE_REQUIRES_DB_BACKUP=1
    if classification="$(python3 "$pkg/scripts/updater.py" classify \
        --current "$INSTALL_DIR" --incoming "$pkg" 2>/dev/null)"; then
        RELEASE_REQUIRES_DB_BACKUP="$(python3 -c \
            'import json,sys; print(1 if json.loads(sys.argv[1]).get("requiresRestart") else 0)' \
            "$classification" 2>/dev/null || echo 1)"
    else
        warn "无法分类本机与发行包差异，按后端重启升级准备数据库备份"
    fi
}

prepare_release_upgrade() {
    local pkg="$1" txn="$INSTALL_DIR/.openbear-install-transaction" rel unit
    local -a existing=()
    if [[ -e "$txn" || -L "$txn" ]]; then
        err "检测到未完成的升级事务，拒绝覆盖恢复材料: $txn"
        return 1
    fi
    classify_release_database_backup_need "$pkg"
    if [[ "$RELEASE_REQUIRES_DB_BACKUP" -eq 1 ]]; then
        info "检测到后端重启升级；停服后、切换前将创建 SQLite 一致性备份"
    else
        info "实际文件差异仅需前端刷新/无需重启；不创建数据库备份"
    fi
    mkdir -p "$txn/new"
    cp -a "$pkg/." "$txn/new/" || { rm -rf "$txn"; return 1; }
    if ! python3 "$txn/new/scripts/release_validation.py" retain-assets \
        --incoming "$txn/new/web/dist" --current "$INSTALL_DIR/web/dist"; then
        rm -rf "$txn"
        return 1
    fi
    if ! prepare_release_dependencies "$txn/new"; then
        rm -rf "$txn"
        return 1
    fi

    for rel in app web/dist prompts scripts pyproject.toml uv.lock openbear.service \
        openbear.json.example README.md README release-meta.json .venv; do
        [[ -e "$INSTALL_DIR/$rel" ]] && existing+=("$rel")
    done
    if [[ "${#existing[@]}" -gt 0 ]]; then
        tar -czf "$txn/old-code.tgz" -C "$INSTALL_DIR" -- "${existing[@]}" || {
            rm -rf "$txn"
            return 1
        }
    else
        tar -czf "$txn/old-code.tgz" --files-from /dev/null || {
            rm -rf "$txn"
            return 1
        }
    fi
    unit="$SYSTEMD_UNIT_DIR/$SERVICE_NAME"
    if [[ -e "$unit" ]]; then
        cp -a "$unit" "$txn/old-unit" || { rm -rf "$txn"; return 1; }
    fi
    if systemctl is-active --quiet "$SERVICE_NAME"; then RELEASE_SERVICE_WAS_ACTIVE=1; else RELEASE_SERVICE_WAS_ACTIVE=0; fi
    if systemctl is-enabled --quiet "$SERVICE_NAME"; then RELEASE_SERVICE_WAS_ENABLED=1; else RELEASE_SERVICE_WAS_ENABLED=0; fi
    RELEASE_TXN="$txn"
    ok "代码、前端、依赖和回滚备份均已准备；尚未停止服务或切换文件"
}

restore_release_upgrade() {
    local txn="$1" reason="$2" rel unit="$SYSTEMD_UNIT_DIR/$SERVICE_NAME"
    warn "升级失败，恢复旧版本: $reason"
    if ! systemctl stop "$SERVICE_NAME" >/dev/null 2>&1; then
        err "恢复前无法停止 $SERVICE_NAME；未删除任何当前文件，备份保留在 $txn"
        return 1
    fi
    for rel in app web/dist prompts scripts pyproject.toml uv.lock openbear.service \
        openbear.json.example README.md README release-meta.json .venv; do
        if ! rm -rf "$INSTALL_DIR/$rel"; then
            err "清理新版本 $rel 失败；备份保留在 $txn"
            return 1
        fi
    done
    if [[ ! -f "$txn/old-code.tgz" ]]; then
        err "缺少旧代码/依赖备份；事务目录保留在 $txn"
        return 1
    fi
    if ! tar -xzf "$txn/old-code.tgz" -C "$INSTALL_DIR"; then
        err "旧代码/依赖恢复失败；唯一备份保留在 $txn，请人工恢复"
        return 1
    fi
    if [[ -f "$txn/old-unit" ]]; then
        if ! mkdir -p "$SYSTEMD_UNIT_DIR" || ! cp -a "$txn/old-unit" "$unit"; then
            err "旧 systemd unit 恢复失败；事务目录保留在 $txn"
            return 1
        fi
    elif ! rm -f "$unit"; then
        err "移除新 systemd unit 失败；事务目录保留在 $txn"
        return 1
    fi
    if ! systemctl daemon-reload >/dev/null 2>&1; then
        err "systemd daemon-reload 失败；事务目录保留在 $txn"
        return 1
    fi
    if [[ "$RELEASE_SERVICE_WAS_ENABLED" -eq 1 ]]; then
        if ! systemctl enable "$SERVICE_NAME" >/dev/null 2>&1; then
            err "恢复服务 enable 状态失败；事务目录保留在 $txn"
            return 1
        fi
    elif ! systemctl disable "$SERVICE_NAME" >/dev/null 2>&1; then
        err "恢复服务 disable 状态失败；事务目录保留在 $txn"
        return 1
    fi
    if [[ "$RELEASE_SERVICE_WAS_ACTIVE" -eq 1 ]] && ! systemctl start "$SERVICE_NAME" >/dev/null 2>&1; then
        err "旧服务恢复启动失败；事务目录保留在 $txn"
        return 1
    fi
    if ! rm -rf "$txn"; then
        err "旧版本已恢复，但事务目录清理失败: $txn"
        return 1
    fi
    RELEASE_TXN=""
    ok "旧版本代码、依赖、unit 和服务状态已恢复"
    if [[ -n "${RELEASE_DB_BACKUP_PATH:-}" ]]; then
        warn "数据库未自动回退；升级前备份保留在 $RELEASE_DB_BACKUP_PATH。人工恢复会丢失备份后的新数据"
    fi
    return 0
}

backup_release_database() {
    local stage="$1" output py
    py="$stage/.venv/bin/python"
    RELEASE_DB_BACKUP_PATH=""
    [[ "$RELEASE_REQUIRES_DB_BACKUP" -eq 1 ]] || return 0
    [[ -x "$py" ]] || { err "staging Python 不存在，无法创建数据库备份: $py"; return 1; }
    if ! output="$("$py" "$stage/scripts/release_validation.py" backup-database \
        --root "$INSTALL_DIR" \
        --backup-dir "$INSTALL_DIR/data/backups" \
        --label "v${RELEASE_VERSION:-unknown}")"; then
        err "升级前数据库一致性备份失败: ${output:-无详情}"
        return 1
    fi
    RELEASE_DB_BACKUP_PATH="$("$py" -c \
        'import json,sys; print((json.loads(sys.argv[1]) or {}).get("backupPath") or "")' \
        "$output")" || return 1
    if [[ -n "$RELEASE_DB_BACKUP_PATH" ]]; then
        ok "数据库一致性备份已保留: $RELEASE_DB_BACKUP_PATH"
    else
        info "未发现旧数据库，无需创建升级前备份"
    fi
}

commit_release_upgrade() {
    local txn="$1" reason=""
    section "停止服务并切换已准备的新版本"
    if ! systemctl stop "$SERVICE_NAME"; then
        err "无法停止 $SERVICE_NAME，未切换任何文件；staging 与备份保留在 $txn"
        if [[ "$RELEASE_SERVICE_WAS_ACTIVE" -eq 1 ]]; then
            systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
        fi
        return 1
    fi
    if ! backup_release_database "$txn/new"; then
        err "数据库备份失败，未切换任何文件"
        if [[ "$RELEASE_SERVICE_WAS_ACTIVE" -eq 1 ]]; then
            if ! systemctl start "$SERVICE_NAME"; then
                err "旧服务恢复启动失败；staging 与代码备份保留在 $txn"
                return 2
            fi
        fi
        return 1
    fi
    if ! copy_release_tree "$txn/new"; then
        reason="切换代码失败"
    elif [[ ! -x "$txn/new/.venv/bin/python" ]]; then
        reason="staging 依赖环境缺失"
    else
        rm -rf "$INSTALL_DIR/.venv"
        if ! mv "$txn/new/.venv" "$INSTALL_DIR/.venv"; then
            reason="切换依赖环境失败"
        elif ! write_service; then
            reason="安装 systemd unit 失败"
        elif ! systemctl start "$SERVICE_NAME"; then
            reason="启动新版本服务失败"
        elif ! verify_web_ready "$RELEASE_VERSION"; then
            reason="新版本 Web 健康检查失败"
        fi
    fi
    if [[ -n "$reason" ]]; then
        if ! restore_release_upgrade "$txn" "$reason"; then
            err "自动恢复未完成；不要删除事务目录，恢复材料保留在 $txn"
            return 2
        fi
        return 1
    fi
    rm -rf "$txn"
    RELEASE_TXN=""
    ok "发行版事务切换完成"
}

sync_code_release() {
    local tmp zip_path sums_path extract pkg
    tmp="$(mktemp -d)"
    zip_path="$tmp/openbear-${RELEASE_VERSION}.zip"
    info "下载发行包 $RELEASE_TAG"
    curl -fL --retry 3 -A "OpenBear-Install" -o "$zip_path" "$RELEASE_ZIP_URL" || die "下载发行包失败"
    [[ -n "${RELEASE_SUMS_URL:-}" ]] || die "发行版缺少 SHA256SUMS，拒绝安装或升级"
    sums_path="$tmp/SHA256SUMS"
    curl -fL --retry 3 -A "OpenBear-Install" -o "$sums_path" "$RELEASE_SUMS_URL" || die "下载 SHA256SUMS 失败"
    python3 - "$zip_path" "$sums_path" <<'PY'
import hashlib, re, sys
from pathlib import Path
zip_path, sums_path = Path(sys.argv[1]), Path(sys.argv[2])
want = zip_path.name
expected = ""
for line in sums_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    parts = line.replace(" *", "  ").split()
    if len(parts) >= 2 and Path(parts[-1]).name == want:
        expected = parts[0].lower()
        break
if not re.fullmatch(r"[0-9a-f]{64}", expected):
    raise SystemExit(f"SHA256SUMS 中 {want} 的摘要缺失或无效")
digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
if digest != expected:
    raise SystemExit(f"SHA256 不匹配: {digest} != {expected}")
print(digest)
PY
    ok "SHA256 校验通过"
    extract="$tmp/extract"
    mkdir -p "$extract"
    python3 - "$zip_path" "$extract" <<'PY'
import sys, zipfile
from pathlib import Path
zip_path, dest = Path(sys.argv[1]), Path(sys.argv[2])
dest_res = dest.resolve()
with zipfile.ZipFile(zip_path) as zf:
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if target != dest_res and dest_res not in target.parents:
            raise SystemExit(f"unsafe zip path: {info.filename}")
    zf.extractall(dest)
PY
    pkg="$extract"
    if [[ ! -d "$pkg/app" && ! -d "$pkg/web/dist" ]]; then
        local kids=()
        mapfile -t kids < <(find "$extract" -mindepth 1 -maxdepth 1 ! -name '__MACOSX' ! -name '.DS_Store')
        if [[ "${#kids[@]}" -eq 1 && -d "${kids[0]}" ]]; then
            pkg="${kids[0]}"
        fi
    fi
    if [[ "$MODE" == "fresh" && ! -d "$INSTALL_DIR/app" ]]; then
        mkdir -p "$(dirname "$INSTALL_DIR")"
        if [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]]; then
            die "$INSTALL_DIR 非空且不是 OpenBear 安装，拒绝覆盖"
        fi
        rm -rf "$INSTALL_DIR"
        mkdir -p "$INSTALL_DIR"
    elif [[ "$MODE" != "fresh" && -d "$INSTALL_DIR/.git" && -n "$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null || true)" ]]; then
        git -C "$INSTALL_DIR" status --porcelain | sed 's/^/    /'
        die "安装目录有未提交改动，已拒绝升级以免覆盖本地修改"
    fi
    validate_release_package "$pkg" || die "发行包不完整，未切换任何文件"
    if [[ "$MODE" == "upgrade" ]]; then
        prepare_release_upgrade "$pkg" || die "新版本 staging 或依赖准备失败，旧服务和文件未改变"
        RELEASE_UPGRADE_PENDING=1
        rm -rf "$tmp"
        return 0
    fi
    python3 "$pkg/scripts/release_validation.py" retain-assets \
        --incoming "$pkg/web/dist" || die "无法写入前端资源代际信息"
    copy_release_tree "$pkg" || die "复制发行版失败"
    INSTALL_DIR="$INSTALL_DIR" REPO_REF="$REPO_REF" write_install_source release "$RELEASE_VERSION" "$RELEASE_TAG"
    rm -rf "$tmp"
    ok "已安装发行版 $RELEASE_TAG"
}

sync_code_git() {
    section "同步代码 (git $REPO_REF)"
    if [[ "$MODE" == "fresh" ]]; then
        if [[ -d "$INSTALL_DIR/.git" ]]; then
            info "目录已是 git 仓库，改为拉取"
        else
            mkdir -p "$(dirname "$INSTALL_DIR")"
            if [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]]; then
                die "$INSTALL_DIR 非空且不是 OpenBear 仓库，拒绝覆盖"
            fi
            rm -rf "$INSTALL_DIR"
            git clone --branch "$REPO_REF" --depth 1 "$REPO_URL" "$INSTALL_DIR"
            INSTALL_DIR="$INSTALL_DIR" REPO_REF="$REPO_REF" write_install_source git
            ok "已克隆 $REPO_URL ($REPO_REF)"
            return 0
        fi
    fi

    cd "$INSTALL_DIR"
    if [[ ! -d .git ]]; then
        die "$INSTALL_DIR 不是 git 仓库，无法走 git 升级。请等待正式发行版，或设置 OPENBEAR_SOURCE=git 前先用 git 安装。"
    fi
    if [[ -n "$(git status --porcelain)" ]]; then
        git status --porcelain | sed 's/^/    /'
        die "安装目录有未提交改动，已拒绝升级以免覆盖本地修改"
    fi
    git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REPO_URL"
    git fetch --tags origin "$REPO_REF"
    if git show-ref --verify --quiet "refs/remotes/origin/${REPO_REF}"; then
        git checkout -B "$REPO_REF" "origin/${REPO_REF}"
        git reset --hard "origin/${REPO_REF}"
    else
        git checkout -B "$REPO_REF" "FETCH_HEAD"
        git reset --hard FETCH_HEAD
    fi
    INSTALL_DIR="$INSTALL_DIR" REPO_REF="$REPO_REF" write_install_source git "" ""
    ok "代码已更新到 $(git rev-parse --short HEAD)"
}

sync_code() {
    if [[ "${OPENBEAR_SOURCE:-release}" == "git" ]]; then
        sync_code_git
        return
    fi
    local meta
    meta="$(mktemp)"
    if fetch_latest_release_meta "$meta"; then
        # shellcheck disable=SC1090
        source "$meta"
        rm -f "$meta"
        section "同步发行包 ($RELEASE_TAG)"
        sync_code_release
        return
    fi
    rm -f "$meta"
    if [[ "$MODE" == "upgrade" ]]; then
        die "无法取得可校验的 GitHub 稳定发行版；升级未改动文件，也不会自动切换到 git"
    fi
    warn "没有可用的 GitHub 稳定发行版，回退到 git $REPO_REF"
    sync_code_git
}

sync_python() {
    section "Python 依赖"
    cd "$INSTALL_DIR"
    export PATH="${HOME}/.local/bin:/root/.local/bin:${PATH}"
    local py="3.12"
    if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
        py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    else
        info "系统 Python < 3.11，改用 uv 安装 3.12"
    fi
    if [[ -f uv.lock ]]; then
        uv sync --frozen --python "$py" || uv sync --python "$py"
    else
        uv sync --python "$py"
    fi
    [[ -x "$INSTALL_DIR/.venv/bin/python" ]] || die "未找到 $INSTALL_DIR/.venv/bin/python"
    ok "venv: $("$INSTALL_DIR/.venv/bin/python" -V)"
}

build_web() {
    if [[ -f "$INSTALL_DIR/web/dist/index.html" && "${OPENBEAR_FORCE_BUILD_WEB:-}" != "1" ]]; then
        section "Web 前端"
        info "发行包已包含 web/dist，跳过 Node 检查与前端构建"
        return 0
    fi
    section "构建 Web 前端"
    ensure_node
    cd "$INSTALL_DIR/web"
    if [[ -f package-lock.json ]]; then
        npm ci
    else
        npm install
    fi
    npm run build
    [[ -f "$INSTALL_DIR/web/dist/index.html" ]] || die "web/dist/index.html 未生成"
    ok "前端已构建"
}

write_runtime() {
    section "写入配置与目录"
    cd "$INSTALL_DIR"
    mkdir -p data workspace skills mcp-servers data/media/inbound

    if [[ "$MODE" == "upgrade" ]]; then
        info "保留已有 openbear.json"
        return 0
    fi

    if [[ "$MODE" == "overwrite" && -f openbear.json ]]; then
        local ts
        ts="$(date +%Y%m%d-%H%M%S)"
        cp -a openbear.json "openbear.json.bak.${ts}"
        ok "已备份 openbear.json.bak.${ts}"
    fi

    DISPLAY_NAME="${DISPLAY_NAME:-$DEFAULT_NAME}"
    MODEL_NAME="${MODEL_NAME:-$MODEL_ID}"
    MODEL_PROVIDER="${MODEL_PROVIDER:-default}"
    export _OB_TOKEN="$TG_TOKEN"
    export _OB_ADMIN="$TG_ADMIN"
    export _OB_PNAME="$MODEL_PROVIDER"
    export _OB_BASE="$MODEL_BASE_URL"
    export _OB_KEY="$MODEL_API_KEY"
    export _OB_PROTO="$MODEL_PROTOCOL"
    export _OB_MID="$MODEL_ID"
    export _OB_MNAME="$MODEL_NAME"
    export _OB_PORT="$WEB_PORT"
    export _OB_NAME="$DISPLAY_NAME"
    PYTHONPATH="$INSTALL_DIR" "$INSTALL_DIR/.venv/bin/python" - <<'PY'
import json, os
from pathlib import Path
root = Path(".").resolve()
example = json.loads((root / "openbear.json.example").read_text(encoding="utf-8"))
example["telegram"]["botToken"] = os.environ["_OB_TOKEN"]
example["telegram"]["whitelistIds"] = [int(os.environ["_OB_ADMIN"])]
example["web"]["port"] = int(os.environ["_OB_PORT"])
pname = (os.environ.get("_OB_PNAME") or "default").strip()
provider = example["models"]["providers"].get("default") or next(iter(example["models"]["providers"].values()))
provider = dict(provider)
provider["baseUrl"] = os.environ["_OB_BASE"].rstrip()
provider["apiKey"] = os.environ["_OB_KEY"]
provider["protocol"] = os.environ["_OB_PROTO"]
mid = os.environ["_OB_MID"].strip()
models = list(provider.get("models") or [{}])
if not models:
    models = [{}]
models[0] = dict(models[0])
models[0]["id"] = mid
models[0]["name"] = os.environ.get("_OB_MNAME") or mid
provider["models"] = models
example["models"]["providers"] = {pname: provider}
example["models"]["primary"] = f"{pname}/{mid}"
example["models"]["compressionModels"] = []
(root / "openbear.json").write_text(json.dumps(example, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
meta = {"displayName": os.environ.get("_OB_NAME") or "老大"}
(root / "data" / "install-meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
from app.config import Config
cfg = Config.model_validate(example)
errors = cfg.validate_for_startup()
if errors:
    raise SystemExit("配置校验失败: " + "; ".join(errors))
print("config-ok")
PY
    unset _OB_TOKEN _OB_ADMIN _OB_PNAME _OB_BASE _OB_KEY _OB_PROTO _OB_MID _OB_MNAME _OB_PORT _OB_NAME
    chmod 600 "$INSTALL_DIR/openbear.json"
    ok "已写入 openbear.json（600）与 data/install-meta.json"
}

write_service() {
    section "安装 systemd 服务"
    local unit="${SYSTEMD_UNIT_DIR}/${SERVICE_NAME}"
    mkdir -p "$SYSTEMD_UNIT_DIR" || return 1
    sed "s|__OPENBEAR_DIR__|${INSTALL_DIR}|g" "$INSTALL_DIR/openbear.service" > "$unit" || return 1
    chmod 644 "$unit" || return 1
    systemctl daemon-reload || return 1
    systemctl enable "$SERVICE_NAME" || return 1
    ok "已安装 $unit"
}

ensure_firewall() {
    local port="${WEB_PORT:-$DEFAULT_PORT}"
    section "防火墙"
    if [[ "${OPENBEAR_SKIP_FIREWALL:-}" == "1" ]]; then
        info "OPENBEAR_SKIP_FIREWALL=1，跳过本机防火墙"
        return 0
    fi
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "Status: active"; then
        if ufw status | grep -qE "(^|[[:space:]])${port}/tcp"; then
            ok "ufw 已放行 ${port}/tcp"
        elif [[ "$NONINTERACTIVE" -eq 1 ]] || confirm_tty "检测到 ufw 正在拦截入站，是否放行 ${port}/tcp？" Y; then
            ufw allow "${port}/tcp" comment "OpenBear Web"
            ok "ufw 已放行 ${port}/tcp"
        else
            warn "未放行 ${port}/tcp。本机以外可能打不开 Web"
        fi
        return 0
    fi
    if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state 2>/dev/null | grep -qi running; then
        if firewall-cmd --list-ports 2>/dev/null | grep -qE "(^|[[:space:]])${port}/tcp"; then
            ok "firewalld 已放行 ${port}/tcp"
        elif [[ "$NONINTERACTIVE" -eq 1 ]] || confirm_tty "检测到 firewalld 正在运行，是否放行 ${port}/tcp？" Y; then
            firewall-cmd --permanent --add-port="${port}/tcp"
            firewall-cmd --reload
            ok "firewalld 已放行 ${port}/tcp"
        else
            warn "未放行 ${port}/tcp。本机以外可能打不开 Web"
        fi
        return 0
    fi
    info "未检测到活动的 ufw/firewalld。若云厂商安全组未放行 ${port}，请自行打开"
}

verify_web_ready() {
    local expected_version="$1" i output
    local health_url="http://127.0.0.1:${WEB_PORT:-$DEFAULT_PORT}/health"
    for i in $(seq 1 30); do
        if output="$("$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/release_validation.py" \
            probe-web --health-url "$health_url" --version "$expected_version" --timeout 4 2>&1)"; then
            if systemctl is-active --quiet "$SERVICE_NAME"; then
                ok "健康检查、/login 与入口资源检查通过: $output"
                return 0
            fi
            output="服务未处于 active"
        fi
        sleep 2
    done
    err "60s 内 Web 验收未通过: ${output:-无响应}"
    journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
    return 1
}

start_and_verify() {
    local expected_version="${1:-}"
    section "启动并验证"
    systemctl restart "$SERVICE_NAME" || die "重启 $SERVICE_NAME 失败"
    verify_web_ready "$expected_version" || die "新版本 Web 验收失败"
    print_done
}

print_done() {
    local port="${WEB_PORT:-}"
    if [[ -z "$port" && -f "$INSTALL_DIR/openbear.json" ]]; then
        port="$("$INSTALL_DIR/.venv/bin/python" -c 'import json; print(json.load(open("openbear.json"))["web"]["port"])' 2>/dev/null || true)"
    fi
    port="${port:-$DEFAULT_PORT}"
    detect_urls "$port"
    read_web_secret || true
    local secret_line external_line
    if [[ -n "${WEB_SECRET:-}" ]]; then
        secret_line="$WEB_SECRET"
    else
        secret_line="（未读到，请用 Admin 账号给 Bot 发 /web 查看）"
    fi
    if [[ -n "${EXTERNAL_URL:-}" ]]; then
        external_line="$EXTERNAL_URL"
    else
        external_line="（未检测到公网 IP，请检查云厂商安全组后自行拼 http://<公网IP>:${port}）"
    fi
    cat <<EOF

${C_GREEN}${C_BOLD}╔════════════════════════════════════╗
║         部署完成                   ║
╚════════════════════════════════════╝${C_RESET}

  目录     : ${INSTALL_DIR}
  工作区   : ${INSTALL_DIR}/workspace
  服务     : ${SERVICE_NAME}

  内网地址 : ${INTERNAL_URL}
  外网地址 : ${external_line}
  访问密钥 : ${secret_line}
$(if [[ -n "${RELEASE_DB_BACKUP_PATH:-}" ]]; then printf '  数据库备份 : %s\n' "$RELEASE_DB_BACKUP_PATH"; fi)
第一次登录：
  1. 用上面的 Telegram Admin 账号给 Bot 发一次 /start
  2. 浏览器打开内网或外网地址
  3. 粘贴访问密钥
  4. 回 Telegram 点确认登录

  若外网打不开：检查云厂商安全组是否放行 ${port}/tcp。
  不想改本机防火墙：OPENBEAR_SKIP_FIREWALL=1 bash scripts/install.sh

常用命令：
  systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
  bash ${INSTALL_DIR}/scripts/install.sh    # 再跑一遍即升级
  bash ${INSTALL_DIR}/scripts/uninstall.sh

EOF
}

main() {
    print_banner
    need_root
    need_linux
    ensure_bootstrap_packages
    collect_config
    ensure_apt_packages
    ensure_uv
    sync_code
    if [[ "$RELEASE_UPGRADE_PENDING" -eq 1 ]]; then
        # 事务升级的依赖已在 staging 中准备；先完成所有无需停服的本机工作。
        if [[ -z "${WEB_PORT:-}" ]]; then
            WEB_PORT="$("$INSTALL_DIR/.venv/bin/python" -c 'import json; print(json.load(open("openbear.json"))["web"]["port"])')"
        fi
        ensure_firewall
        commit_release_upgrade "$RELEASE_TXN" || die "发行版升级失败，已尝试恢复旧版本"
        INSTALL_DIR="$INSTALL_DIR" REPO_REF="$REPO_REF" write_install_source release "$RELEASE_VERSION" "$RELEASE_TAG"
        ok "已升级到发行版 $RELEASE_TAG"
        print_done
        return 0
    fi
    sync_python
    build_web
    write_runtime
    write_service
    # 升级时从已有配置读端口做 health / 防火墙
    if [[ "$MODE" == "upgrade" && -z "${WEB_PORT:-}" ]]; then
        WEB_PORT="$("$INSTALL_DIR/.venv/bin/python" -c 'import json; print(json.load(open("openbear.json"))["web"]["port"])')"
    fi
    ensure_firewall
    start_and_verify "${RELEASE_VERSION:-}"
}

main "$@"
