"""The server benchmarks: which tests exist, the package version each runs, their options,
what they warn about, and the command a node runs for them.

collect says where a run's report comes from:
  files     report-<4|6>.ansi, result-<4|6>.json and link-<4|6>.txt in the output directory
  envelope  envelope.json in the output directory: {"terminal": ANSI, "report": JSON}
  log       the job's output after its last screen clear
"""
from __future__ import annotations

import shlex

from app.instances.contracts import InstanceError

# The package version each tool runs; benchmark-package/<tool>/<version> must exist.
PACKAGES = {
    'ip.check.place': 'v0.1.0',
    'net.check.place': 'v0.1.0',
    'hardware.check.place': 'v0.1.0',
    'tcpquality': 'v0.1.0',
    'service-unlock': 'v0.1.0',
    'geekbench5': 'v0.1.0',
}
NODE_ROOT = '/var/tmp/openbear-bench'
# Every package directory on a node holds this list of its files' SHA256; a job checks it first
# and exits with CHECKSUM_FAILED when the copy there was changed or cut short.
LISTING = '.openbear-files.sha256'
CHECKSUM_FAILED = 86

CATEGORIES = [('ip', 'IP 质量'), ('network', '网络质量'), ('hardware', '硬件性能'), ('media', '流媒体与 AI 解锁')]

FAMILY = {'key': 'family', 'label': '协议', 'type': 'choice', 'default': '4',
          'choices': [['4', '仅 IPv4'], ['6', '仅 IPv6'], ['46', 'IPv4 + IPv6']]}
REPORT = {'key': 'report', 'label': '生成在线报告', 'type': 'bool', 'default': False}
PROVINCES = ['北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江', '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南',
             '湖北', '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆']
UNLOCK_REGIONS = [['auto', '自动（按出口所在地区）'], ['global', '只测全球通用项目'], ['hk', '香港'], ['tw', '台湾'], ['jp', '日本'], ['kr', '韩国'],
                  ['sea', '东南亚'], ['na', '北美'], ['sa', '南美'], ['eu', '欧洲'], ['oc', '大洋洲'], ['af', '非洲']]
TCPQ_MODES = {'v4': ('三网质量 · IPv4', ['-v4']), 'v6': ('三网质量 · IPv6', ['-v6']),
              'route-v4': ('回程线路 · IPv4', ['--route', '-v4']), 'route-v6': ('回程线路 · IPv6', ['--route', '-v6']),
              'intl': ('国际互联', ['--intl']), 'speedtest': ('三网测速', ['--only-speedtest']),
              'all': ('完整检测', ['-v4', '-v6', '--intl', '--speedtest'])}

UPLOAD_WARNING = {'text': '结果会上传到 Report.Check.Place，生成任何人都能打开的报告链接。', 'if': {'report': [True]}}

TESTS = {
    'ipcheck': {
        'name': 'IP.Check.Place', 'category': 'ip', 'package': 'ip.check.place', 'collect': 'files', 'duration': '1～3 分钟',
        'summary': 'IP 类型、多家数据库的风险评分、流媒体与 AI 解锁、邮局连通性和黑名单。',
        'options': [FAMILY, {**REPORT, 'help': '把结果上传到 Report.Check.Place，生成公开报告链接'}],
        'warnings': [{'text': '会用系统包管理器安装缺少的依赖（jq、curl、bc、dnsutils 等）。'}, UPLOAD_WARNING],
    },
    'netcheck': {
        'name': 'Net.Check.Place', 'category': 'network', 'package': 'net.check.place', 'collect': 'files',
        'duration': '低数据模式约 8 分钟，完整模式 15 分钟以上',
        'summary': 'BGP 信息、NAT 与 TCP 参数、三网 TCP 大包延迟、三网回程、国内测速和国际互连。',
        'options': [FAMILY,
                    {'key': 'mode', 'label': '模式', 'type': 'choice', 'default': 'low',
                     'choices': [['low', '低数据模式', '不跑三网测速'], ['full', '完整模式', '含三网测速和国际互连带宽']]},
                    {**REPORT, 'help': '把结果上传到 Report.Check.Place，生成公开报告链接'}],
        'warnings': [{'text': '会用系统包管理器安装缺少的依赖（jq、curl、imagemagick、mtr、iperf3、bc 等）。'},
                     {'text': '三网测速和国际互连带宽测试会消耗大量流量，带宽越大用得越多：千兆以上的机器一次可能超过 10 GB。', 'if': {'mode': ['full']}},
                     UPLOAD_WARNING],
    },
    'netroute': {
        'name': 'Net.Check.Place 回程路由', 'category': 'network', 'package': 'net.check.place', 'collect': 'files', 'duration': '约 5 分钟',
        'summary': '到指定省份三网的完整回程路由（TCP 大包），逐跳显示线路。',
        'options': [FAMILY,
                    {'key': 'province', 'label': '省份', 'type': 'choice', 'default': '',
                     'choices': [['', '北京、上海、广东']] + [[p, p] for p in PROVINCES]}],
        'warnings': [{'text': '会用系统包管理器安装缺少的依赖（jq、curl、imagemagick、mtr、iperf3、bc 等）。'}],
    },
    'tcpquality': {
        'name': 'TCPQuality', 'category': 'network', 'package': 'tcpquality', 'collect': 'log', 'duration': '3～15 分钟，取决于模式',
        'summary': '全国三网 TCP 延迟与重传、回程线路识别、国际互联和国内单线程测速。',
        'options': [{'key': 'mode', 'label': '模式', 'type': 'choice', 'default': 'v4', 'choices': [[k, v[0]] for k, v in TCPQ_MODES.items()]},
                    {**REPORT, 'help': '上传到 tcpquality.ibsgss.uk 生成公开报告，并参与速度排名；回程线路模式不上传'}],
        'warnings': [{'text': '会用系统包管理器安装缺少的依赖（curl、nmap、iperf3、traceroute、jq 等）。'},
                     {'text': '测试节点列表由 TCPQuality 作者的服务提供，运行时需要访问 tcpquality.ibsgss.uk。'},
                     {'text': '三网测速会消耗大量流量，通常在数 GB。', 'if': {'mode': ['speedtest', 'all']}},
                     {'text': '结果会上传到 tcpquality.ibsgss.uk，生成任何人都能打开的报告链接。', 'if': {'report': [True]}}],
    },
    'hwcheck': {
        'name': 'Hardware.Check.Place', 'category': 'hardware', 'package': 'hardware.check.place', 'collect': 'files', 'duration': '3～8 分钟',
        'summary': '系统与主板信息，CPU（sysbench）、内存和硬盘（fio）测试。',
        'options': [{**REPORT, 'help': '会同时运行 GB5 和显卡测试，并把结果上传到 Report.Check.Place 生成公开报告链接'}],
        'warnings': [{'text': '会用系统包管理器安装缺少的依赖（dmidecode、smartmontools、fio、sysbench 等）。'},
                     {'text': 'CPU、内存、硬盘压力测试会占满资源几分钟，硬盘测试会写入临时文件。'},
                     {'text': '会额外运行 GB5（成绩上传到 Geekbench 官网），并把报告上传到 Report.Check.Place。', 'if': {'report': [True]}}],
    },
    'geekbench5': {
        'name': 'Geekbench 5', 'category': 'hardware', 'package': 'geekbench5', 'collect': 'files', 'duration': '约 5 分钟',
        'summary': 'Geekbench 5 单核与多核跑分。',
        'options': [],
        'warnings': [{'text': '免费版的成绩必须上传到 Geekbench 官网（browser.geekbench.com）才能取得，任何人都能看到。'},
                     {'text': '可用内存不足 1.5 GB 时会临时创建 2 GB swap 文件，结束后删除。'},
                     {'text': '跑分期间会占满 CPU。'}],
    },
    'unlock': {
        'name': 'Service Unlock', 'category': 'media', 'package': 'service-unlock', 'collect': 'envelope', 'duration': '约 1 分钟',
        'summary': 'Netflix、YouTube Premium、Spotify、ChatGPT、Claude、Gemini 等流媒体与 AI 服务的解锁情况。',
        'options': [{'key': 'region', 'label': '检测地区', 'type': 'choice', 'default': 'auto', 'choices': UNLOCK_REGIONS}],
        'warnings': [],
    },
}

# 综合评测: the four checks NodeQuality bundles, each kept in its own test's history.
SUITE = {'name': '综合评测', 'summary': '依次运行硬件、IP 质量、网络质量（低数据模式）和回程路由四项，不上传。', 'duration': '约 20～30 分钟',
         'options': [FAMILY], 'tests': ['hwcheck', 'ipcheck', 'netcheck', 'netroute'],
         # What its four tests warn about without an upload or a full mode, said once.
         'warnings': [{'text': '会用系统包管理器安装缺少的依赖（jq、curl、bc、dnsutils、imagemagick、mtr、iperf3、dmidecode、smartmontools、fio、sysbench 等）。'},
                      {'text': 'CPU、内存、硬盘压力测试会占满资源几分钟，硬盘测试会写入临时文件。'}]}


def view():
    """The catalogue as the page shows it."""
    return {
        'categories': [{'id': key, 'name': name} for key, name in CATEGORIES],
        'tests': [{'id': key, **{k: t[k] for k in ('name', 'category', 'duration', 'summary', 'options', 'warnings')},
                   'package': t['package'], 'version': PACKAGES[t['package']]} for key, t in TESTS.items()],
        'suite': {k: SUITE[k] for k in ('name', 'summary', 'duration', 'options', 'tests', 'warnings')},
    }


def options(test, raw):
    """The test's options, each checked against its definition; missing ones take their default."""
    if test not in TESTS:
        raise InstanceError('bench_unknown_test', 404)
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for spec in TESTS[test]['options']:
        value = raw.get(spec['key'], spec['default'])
        if spec['type'] == 'bool':
            if type(value) is not bool:
                raise InstanceError('bench_invalid_option')
        elif value not in [c[0] for c in spec['choices']]:
            raise InstanceError('bench_invalid_option')
        out[spec['key']] = value
    if set(raw) - set(out):
        raise InstanceError('bench_invalid_option')
    return out


def label(test, opts):
    """A run's options in a few words, for its line in the tree."""
    parts = []
    for spec in TESTS[test]['options']:
        value = opts.get(spec['key'], spec['default'])
        if spec['type'] == 'bool':
            if value:
                parts.append(spec['label'])
        elif value != spec['default'] or spec['key'] == 'mode':
            parts.append(next(c[1] for c in spec['choices'] if c[0] == value))
    return ' · '.join(parts)


def _family(opts):
    return {'4': ['-4'], '6': ['-6'], '46': []}[opts.get('family', '4')]


def arguments(test, opts):
    """The tool's own arguments (after the entry script)."""
    privacy = [] if opts.get('report') else ['-p']
    if test == 'ipcheck':
        return ['-y', *privacy, *_family(opts)]
    if test == 'netcheck':
        return ['-y', *privacy, *_family(opts), *(['-L'] if opts['mode'] == 'low' else [])]
    if test == 'netroute':
        return ['-y', '-p', *_family(opts), '-R', *([opts['province']] if opts['province'] else []), '-S', '123']
    if test == 'tcpquality':
        mode = TCPQ_MODES[opts['mode']][1]
        return [*mode, *([] if opts.get('report') and '--route' not in mode else ['--no-rank-upload'])]
    if test == 'hwcheck':
        return ['-y', *privacy]
    if test == 'unlock':
        return ['--machine', *(['--region', opts['region']] if opts['region'] != 'auto' else [])]
    return []


def package_dir(test):
    tool = TESTS[test]['package']
    return f'{NODE_ROOT}/{tool}-{PACKAGES[tool]}'


def command(test, opts, out_dir):
    """The job's shell command: the package's variables, then its entry point."""
    base = package_dir(test)
    args = ' '.join(shlex.quote(a) for a in arguments(test, opts))
    if test == 'unlock':
        run = f'"$OB_BENCH_DIR/bin/service-unlock" {args} > "$OB_BENCH_OUT/envelope.json"'
    elif test == 'geekbench5':
        run = 'bash "$OB_BENCH_DIR/run.sh"'
    else:
        entry = {'ipcheck': 'ip.sh', 'netcheck': 'net.sh', 'netroute': 'net.sh', 'tcpquality': 'runTcpQuality-core.sh', 'hwcheck': 'hardware.sh'}[test]
        run = f'bash "$OB_BENCH_DIR/{entry}" {args}'
    return (f'export OB_BENCH_DIR={shlex.quote(base)} OB_BENCH_OUT={shlex.quote(out_dir)} TERM=xterm-256color LANG=C.UTF-8; '
            'export PATH="$OB_BENCH_DIR/bin:$PATH"; '
            f'find {NODE_ROOT}/runs -mindepth 1 -maxdepth 1 -mtime +0 -exec rm -rf {{}} + 2>/dev/null; '
            f'cd "$OB_BENCH_DIR" && sha256sum -c --quiet {LISTING} >/dev/null 2>&1 || '
            f'{{ echo "评测包文件校验失败，下次运行时重新推送" >&2; exit {CHECKSUM_FAILED}; }}; '
            f'mkdir -p "$OB_BENCH_OUT" && cd "$OB_BENCH_OUT" && {run}')
