"""Which streaming and AI services serve a server, and as which country, for its card. The checks
live here: the node only makes the HTTPS requests listed below through one address family
(net.http) and sends back what each pattern found; the verdicts are made here. A change to a
check is a change to this file, with no node update. Results: per service ok, limited (Netflix
originals only), blocked, or error when the answers prove nothing either way."""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlsplit

from app.instances.contracts import InstanceError
from app.servers.checks import ServerChecks
from app.webhooks.repository import one

# Bump when the checks or verdicts change: summaries made by an older one are made again.
VERSION = 1
FEATURE = 'net.http'
SERVICES = ('google', 'youtube', 'openai', 'anthropic', 'gemini', 'netflix', 'spotify', 'reddit', 'tiktok')
# Each server every six hours, and as soon as its address changes.
EVERY_S = 6 * 3600

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
PAGE = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
# Google's consent page would stand in for YouTube's own outside the EU's reach too.
CONSENT = 'SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjUwNzMwLjA1X3AwGgJlbiACGgYIgPC_xAY'
# Cloudflare's interstitial: the service behind it never saw the request.
CHALLENGE = ('cf_chl_opt', '_cf_chl', 'just a moment', 'cf-browser-verification', 'enable javascript and cookies to continue')
NETFLIX_LICENSED, NETFLIX_ORIGINAL = '70143836', '80197526'
GEMINI_REFUSED = {'RU', 'BY', 'CN', 'KP', 'IR', 'CU', 'SY'}
SPOTIFY_REFUSED = {'CN', 'RU', 'BY', 'KP', 'IR', 'CU', 'SY'}


def _request(rid, url, *, headers=None, find=None, contains=None, json_paths=None, method='GET', body=''):
    """find: {name: (pattern, last)}; contains: {name: [texts]} (any of them); json_paths: {name: path}."""
    find, contains, json_paths = find or {}, {**(contains or {}), 'challenge': CHALLENGE}, json_paths or {}
    texts = [(name, text) for name, group in contains.items() for text in group]
    return {'id': rid, 'url': url, 'method': method, 'body': body, 'headers': {**PAGE, **(headers or {})},
            'find': [{'re': pattern, 'last': last} for pattern, last in find.values()],
            'contains': [text for _, text in texts], 'json': list(json_paths.values()),
            '_names': (list(find), [name for name, _ in texts], list(json_paths))}


_REQUESTS = [
    _request('google', 'https://www.google.com/', find={'locale': (r'"[a-z]{2}_([A-Z]{2})"', False), 'dash': (r'"[a-z]{2}-([A-Z]{2})"', True)}),
    _request('youtube', 'https://www.youtube.com/premium', headers={'Cookie': CONSENT},
             find={'region': (r'"contentRegion":"([A-Z]{2})"', False), 'country': (r'"countryCode":"([A-Z]{2})"', False)},
             contains={'refused': ['youtube premium is not available in your country'], 'offer': ['ad-free'], 'cn': ['www.google.cn']}),
    _request('openai', 'https://api.openai.com/compliance/cookie_requirements', contains={'refused': ['unsupported_country']}),
    _request('openai.trace', 'https://chatgpt.com/cdn-cgi/trace', find={'loc': (r'loc=([A-Z]{2})', False)}),
    _request('anthropic', 'https://claude.ai/', headers={'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Site': 'none',
                                                          'Sec-Fetch-User': '?1', 'Upgrade-Insecure-Requests': '1'},
             contains={'refused': ['app unavailable in region', '/app-unavailable-in-region', "unfortunately, claude isn't available here.",
                                   'unfortunately, claude isn&apos;t available here.', 'unfortunately, claude isn&#39;t available here.']}),
    _request('anthropic.trace', 'https://claude.ai/cdn-cgi/trace', find={'loc': (r'loc=([A-Z]{2})', False)}),
    # Cloudflare challenges claude.ai for many datacenter addresses; the API answers them all.
    _request('anthropic.api', 'https://api.anthropic.com/v1/models', headers={'anthropic-version': '2023-06-01'},
             json_paths={'error': 'error.type'}, contains={'refused': ['request not allowed']}),
    _request('gemini', 'https://gemini.google.com/app', find={'region': (r',\d+,\d+,200,"([A-Z]{3})"', False)}),
    _request('netflix', f'https://www.netflix.com/title/{NETFLIX_LICENSED}'),
    _request('netflix.original', f'https://www.netflix.com/title/{NETFLIX_ORIGINAL}'),
    _request('spotify', 'https://accounts.spotify.com/status', find={'region': (r'"geoLocationCountryCode":"([^"]*)"', False)}),
    _request('reddit', 'https://www.reddit.com/', contains={'blocked': ['blocked by network security']}),
    _request('reddit.region', 'https://www.reddit.com/svc/shreddit/reddit-chat', find={'region': (r'country="([A-Za-z]{2})"', False)}),
    _request('tiktok', 'https://www.tiktok.com/', contains={'refused': ['service is currently unavailable in your region', 'tiktok is not available in your country',
                                                                       'tiktok is unavailable in your country', 'not available in your region']}),
    _request('tiktok.region', 'https://www.tiktok.com/api/v1/web-cookie-privacy/config?appId=1988', json_paths={'region': 'body.appProps.region'}),
]
REQUESTS = [{k: v for k, v in r.items() if k != '_names'} for r in _REQUESTS]
_NAMES = {r['id']: r['_names'] for r in _REQUESTS}

# ISO 3166-1 alpha-3 to alpha-2: Gemini names its country in three letters.
_ALPHA3 = dict((p[:3], p[3:]) for p in '''
ABWAW AFGAF AGOAO AIAAI ALAAX ALBAL ANDAD AREAE ARGAR ARMAM ASMAS ATAAQ ATFTF ATGAG AUSAU AUTAT
AZEAZ BDIBI BELBE BENBJ BESBQ BFABF BGDBD BGRBG BHRBH BHSBS BIHBA BLMBL BLRBY BLZBZ BMUBM BOLBO
BRABR BRBBB BRNBN BTNBT BVTBV BWABW CAFCF CANCA CCKCC CHECH CHLCL CHNCN CIVCI CMRCM CODCD COGCG
COKCK COLCO COMKM CPVCV CRICR CUBCU CUWCW CXRCX CYMKY CYPCY CZECZ DEUDE DJIDJ DMADM DNKDK DOMDO
DZADZ ECUEC EGYEG ERIER ESHEH ESPES ESTEE ETHET FINFI FJIFJ FLKFK FRAFR FROFO FSMFM GABGA GBRGB
GEOGE GGYGG GHAGH GIBGI GINGN GLPGP GMBGM GNBGW GNQGQ GRCGR GRDGD GRLGL GTMGT GUFGF GUMGU GUYGY
HKGHK HMDHM HNDHN HRVHR HTIHT HUNHU IDNID IMNIM INDIN IOTIO IRLIE IRNIR IRQIQ ISLIS ISRIL ITAIT
JAMJM JEYJE JORJO JPNJP KAZKZ KENKE KGZKG KHMKH KIRKI KNAKN KORKR KWTKW LAOLA LBNLB LBRLR LBYLY
LCALC LIELI LKALK LSOLS LTULT LUXLU LVALV MACMO MAFMF MARMA MCOMC MDAMD MDGMG MDVMV MEXMX MHLMH
MKDMK MLIML MLTMT MMRMM MNEME MNGMN MNPMP MOZMZ MRTMR MSRMS MTQMQ MUSMU MWIMW MYSMY MYTYT NAMNA
NCLNC NERNE NFKNF NGANG NICNI NIUNU NLDNL NORNO NPLNP NRUNR NZLNZ OMNOM PAKPK PANPA PCNPN PERPE
PHLPH PLWPW PNGPG POLPL PRIPR PRKKP PRTPT PRYPY PSEPS PYFPF QATQA REURE ROURO RUSRU RWARW SAUSA
SDNSD SENSN SGPSG SGSGS SHNSH SJMSJ SLBSB SLESL SLVSV SMRSM SOMSO SPMPM SRBRS SSDSS STPST SURSR
SVKSK SVNSI SWESE SWZSZ SXMSX SYCSC SYRSY TCATC TCDTD TGOTG THATH TJKTJ TKLTK TKMTM TLSTL TONTO
TTOTT TUNTN TURTR TUVTV TWNTW TZATZ UGAUG UKRUA UMIUM URYUY USAUS UZBUZ VATVA VCTVC VENVE VGBVG
VIRVI VNMVN VUTVU WLFWF WSMWS YEMYE ZAFZA ZMBZM ZWEZW'''.split())


def region(value):
    """A country in two letters, or ''."""
    code = str(value or '').strip().upper()
    if re.fullmatch('[A-Z]{3}', code):
        code = _ALPHA3.get(code, '')
    return code if re.fullmatch('[A-Z]{2}', code) and code not in ('XX', 'ZZ', 'T1') else ''


class Answer:
    """One request's answer by name: find, has (any of a group's texts) and json."""

    def __init__(self, raw, names):
        raw = raw if isinstance(raw, dict) else {}
        finds, texts, paths = names
        self.status = raw.get('status') if isinstance(raw.get('status'), int) else 0
        self.url, self.size = str(raw.get('url') or ''), raw.get('size') if isinstance(raw.get('size'), int) else 0
        self.error = str(raw.get('error') or '') or ('' if self.status else 'no_answer')

        def listed(key, n):
            value = raw.get(key)
            return list(value)[:n] + [None] * (n - len(value)) if isinstance(value, list) else [None] * n
        self.find = {name: str(v or '') for name, v in zip(finds, listed('find', len(finds)))}
        self.has = {}
        for name, v in zip(texts, listed('contains', len(texts))):
            self.has[name] = self.has.get(name, False) or v is True
        self.json = {name: str(v or '') for name, v in zip(paths, listed('json', len(paths)))}

    @property
    def ok(self):
        return 200 <= self.status < 400

    def problem(self, *, text=True):
        """Why this answer says nothing about the service, or ''."""
        if self.error:
            return 'timeout' if self.error == 'timeout' else 'failed'
        if self.status in (403, 429, 503) and self.has.get('challenge'):
            return 'challenge'
        if self.status == 429 or self.status >= 500 or self.status < 200:
            return 'http'
        if text and not self.size:
            return 'empty'
        return ''


def _verdict(state, where='', note=''):
    return {'state': state, 'region': region(where), 'note': note}


def _netflix_title(a, title):
    """(region, served) for one title page, or None when the answer is no evidence."""
    if a.problem(text=False):
        return None
    if a.status == 404:
        return '', False
    if not a.ok:
        return None
    parts = urlsplit(a.url)
    if parts.hostname not in ('www.netflix.com', 'netflix.com'):
        return None
    m = re.fullmatch(rf'/(?:([A-Za-z]{{2}})(?:-[A-Za-z]+)?/)?title/{title}/?', parts.path)
    return ((m.group(1) or 'US').upper(), True) if m else None


def judge(results):
    """{service: verdict} from one family's net.http results."""
    by_id = {r.get('id'): r for r in results or [] if isinstance(r, dict)}
    a = {rid: Answer(by_id.get(rid), names) for rid, names in _NAMES.items()}
    out = {}

    yt = a['youtube']
    sent_cn = (urlsplit(yt.url).hostname or '').endswith('google.cn') or yt.has['cn']
    g = a['google']
    google_region = 'CN' if sent_cn else g.find['locale'] or g.find['dash'] or yt.find['region']
    out['google'] = _verdict('error', note=g.problem()) if g.problem() and not sent_cn else \
        _verdict('ok', google_region) if region(google_region) else _verdict('error', note='unclear')

    yt_region = yt.find['region'] or yt.find['country'] or google_region
    if sent_cn:
        out['youtube'] = _verdict('blocked', 'CN')
    elif yt.problem():
        out['youtube'] = _verdict('error', note=yt.problem())
    elif yt.has['refused']:
        out['youtube'] = _verdict('blocked', yt_region)
    elif yt.ok and yt.has['offer']:
        out['youtube'] = _verdict('ok', yt_region)
    else:
        out['youtube'] = _verdict('error', note='unclear')

    o, loc = a['openai'], a['openai.trace'].find['loc']
    out['openai'] = _verdict('error', note=o.problem()) if o.problem() else _verdict('blocked', loc) if o.has['refused'] else \
        _verdict('ok', loc) if o.ok else _verdict('error', note='http')

    c, loc, api = a['anthropic'], a['anthropic.trace'].find['loc'], a['anthropic.api']
    if c.problem() == 'challenge' and api.status == 401 and api.json['error'] == 'authentication_error':
        out['anthropic'] = _verdict('ok', loc, 'api')
    elif c.problem() == 'challenge' and api.status == 403 and (api.json['error'] == 'forbidden' or api.has['refused']):
        out['anthropic'] = _verdict('blocked', loc, 'api')
    elif c.problem():
        out['anthropic'] = _verdict('error', note=c.problem())
    elif c.has['refused'] or '/app-unavailable-in-region' in c.url:
        out['anthropic'] = _verdict('blocked', loc)
    elif c.ok:
        out['anthropic'] = _verdict('ok', loc)
    else:
        # Refused without the page that would put it down to the country: no proof of either.
        out['anthropic'] = _verdict('error', note='http')

    gm = a['gemini']
    where = region(gm.find['region'])
    out['gemini'] = _verdict('error', note=gm.problem()) if gm.problem() else _verdict('error', note='http') if not gm.ok else \
        _verdict('error', note='unclear') if not where else _verdict('blocked' if where in GEMINI_REFUSED else 'ok', where)

    licensed = _netflix_title(a['netflix'], NETFLIX_LICENSED)
    original = _netflix_title(a['netflix.original'], NETFLIX_ORIGINAL) if licensed and not licensed[1] else None
    if licensed is None:
        out['netflix'] = _verdict('error', note=a['netflix'].problem(text=False) or 'unclear')
    elif licensed[1]:
        out['netflix'] = _verdict('ok', licensed[0])
    elif original is None:
        out['netflix'] = _verdict('error', note=a['netflix.original'].problem(text=False) or 'unclear')
    else:
        out['netflix'] = _verdict('limited', original[0], 'originals') if original[1] else _verdict('blocked')

    s = a['spotify']
    where = region(s.find['region'])
    out['spotify'] = _verdict('error', note=s.problem()) if s.problem() else _verdict('error', note='unclear') if not where else \
        _verdict('blocked' if where in SPOTIFY_REFUSED else 'ok', where)

    r, where = a['reddit'], a['reddit.region'].find['region']
    if r.error or (r.status in (403, 429, 503) and r.has['challenge']):
        out['reddit'] = _verdict('error', note=r.problem())
    elif r.has['blocked'] or r.status == 403:
        # Reddit turns away hosting ranges whatever their country.
        out['reddit'] = _verdict('blocked', where, 'datacenter')
    elif r.ok and r.size:
        out['reddit'] = _verdict('ok', where)
    else:
        out['reddit'] = _verdict('error', note=r.problem() or 'http')

    t, where = a['tiktok'], a['tiktok.region'].json['region']
    out['tiktok'] = _verdict('error', note=t.problem()) if t.problem() else _verdict('blocked', where) if t.has['refused'] else \
        _verdict('ok', where) if t.ok else _verdict('error', note='http')
    return out


def addresses(info):
    return [str(info.get('ipv4') or ''), str(info.get('ipv6') or '')]


def now_ms():
    return int(time.time() * 1000)


class UnlockChecks(ServerChecks):
    """Runs the checks on the nodes that have net.http and keeps each server's newest result in
    its node record, under unlock: {v, at, ips, v4?, v6?} (the families it has an address in)."""
    KEY = 'unlock'
    AT_ONCE = 4

    def due(self, server_id, stored, info, zone=None):
        last = stored.get('unlock') or {}
        return last.get('v') != VERSION or last.get('ips') != addresses(info) or now_ms() - (last.get('at') or 0) >= EVERY_S * 1000

    def capable(self, server_id):
        session = self.service.hub.sessions.get(server_id)
        return bool(session and FEATURE in session.features)

    async def check(self, server_id):
        """Run the checks now; the new summary. The record keeps its last one when the node cannot be asked."""
        if server_id not in self.service.hub.sessions:
            raise InstanceError('node_offline', 409)
        if not self.capable(server_id):
            raise InstanceError('node_update_required', 409)
        row = await one(self.db.conn, 'SELECT owner, info_json FROM server_nodes WHERE server_id=?', (server_id,))
        if not row:
            raise InstanceError('node_not_enrolled', 404)
        info = json.loads(row['info_json'] or '{}').get('info') or {}
        ips = addresses(info)
        families = [f for f, ip in ((4, ips[0]), (6, ips[1])) if ip] or [4]

        async def family(f):
            try:
                reply = await self.service._node_call(row['owner'], server_id, 'net.http', {'family': f, 'requests': REQUESTS}, timeout=60)
            except InstanceError as exc:
                if exc.error == 'unsupported_method':
                    raise InstanceError('node_update_required', 409) from None
                raise
            return f'v{f}', judge(reply.get('results'))
        summary = {'v': VERSION, 'at': now_ms(), 'ips': ips, **dict(await asyncio.gather(*(family(f) for f in families)))}
        await self._save(server_id, summary)
        # The detail page's full list is measured each time these are.
        if self.service.unlock_full.capable(server_id):
            self.service.unlock_full.soon(server_id)
        return summary
