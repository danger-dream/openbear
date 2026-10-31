"""Request signing for the providers that need it.

Every function is pure: callers pass the timestamp, so official examples work as test vectors.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote


def encode(value, safe=''):
    """RFC 3986 percent-encoding: only A-Z a-z 0-9 - _ . ~ stay as they are."""
    return quote(str(value), safe='-_.~' + safe)


def query_string(params):
    """Sorted, RFC 3986 encoded query; params is a dict or a list of pairs."""
    items = params.items() if isinstance(params, dict) else params
    return '&'.join(f'{k}={v}' for k, v in sorted((encode(k), encode(v)) for k, v in items))


def sha256_hex(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else (data or b'')).hexdigest()


def _hmac(key, msg, digest=hashlib.sha256):
    return hmac.new(key.encode() if isinstance(key, str) else key, msg.encode(), digest).digest()


def utc(ts, fmt):
    return time.strftime(fmt, time.gmtime(ts))


# ----- Aliyun RPC, signature version 1.0 -----
def aliyun_rpc(method, params, secret):
    """Base64 HMAC-SHA1 over METHOD&%2F&encode(canonical query); params exclude Signature."""
    string_to_sign = f'{method}&{encode("/")}&{encode(query_string(params))}'
    return base64.b64encode(_hmac(secret + '&', string_to_sign, hashlib.sha1)).decode()


# ----- Tencent Cloud TC3-HMAC-SHA256 -----
def tc3(secret_id, secret_key, *, service, host, action, payload, timestamp, content_type='application/json; charset=utf-8'):
    """Headers for a POST to API 3.0; content-type, host and x-tc-action are signed."""
    date = utc(timestamp, '%Y-%m-%d')
    signed = 'content-type;host;x-tc-action'
    canonical_headers = f'content-type:{content_type}\nhost:{host}\nx-tc-action:{action.lower()}\n'
    canonical = f'POST\n/\n\n{canonical_headers}\n{signed}\n{sha256_hex(payload)}'
    scope = f'{date}/{service}/tc3_request'
    string_to_sign = f'TC3-HMAC-SHA256\n{timestamp}\n{scope}\n{sha256_hex(canonical)}'
    key = _hmac('TC3' + secret_key, date)
    for part in (service, 'tc3_request'):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed}, Signature={signature}',
            'Content-Type': content_type, 'Host': host, 'X-TC-Action': action, 'X-TC-Timestamp': str(timestamp)}


# ----- Huawei Cloud APIG SDK-HMAC-SHA256 -----
def huawei(access_key, secret_key, *, method, path, query, headers, body, stamp):
    """Authorization header; every header passed in is signed. stamp is X-Sdk-Date (YYYYMMDDTHHMMSSZ)."""
    names = sorted(k.lower() for k in headers)
    lower = {k.lower(): str(v).strip() for k, v in headers.items()}
    uri = '/'.join(encode(seg) for seg in path.split('/'))
    if not uri.endswith('/'):
        uri += '/'
    canonical = '\n'.join([method, uri, query_string(query), ''.join(f'{k}:{lower[k]}\n' for k in names),
                           ';'.join(names), sha256_hex(body)])
    string_to_sign = f'SDK-HMAC-SHA256\n{stamp}\n{sha256_hex(canonical)}'
    signature = hmac.new(secret_key.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f'SDK-HMAC-SHA256 Access={access_key}, SignedHeaders={";".join(names)}, Signature={signature}'


# ----- Baidu bce-auth-v1 -----
def bce(access_key, secret_key, *, method, path, query, headers, timestamp, expires=1800, list_signed=True):
    """Authorization string; every header passed in is signed (Host is mandatory).

    With list_signed=False the signedHeaders part is left empty, which Baidu reads as its default header set."""
    prefix = f'bce-auth-v1/{access_key}/{utc(timestamp, "%Y-%m-%dT%H:%M:%SZ")}/{expires}'
    items = sorted((k.lower(), str(v).strip()) for k, v in headers.items())
    canonical_headers = '\n'.join(sorted(f'{encode(k)}:{encode(v)}' for k, v in items if v))
    canonical = f'{method}\n{encode(path or "/", "/")}\n{query_string(query)}\n{canonical_headers}'
    signing_key = hmac.new(secret_key.encode(), prefix.encode(), hashlib.sha256).hexdigest()
    signature = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    signed = ';'.join(k for k, _ in items) if list_signed else ''
    return f'{prefix}/{signed}/{signature}'


# ----- AWS SigV4 and Volcengine (same scheme, different names) -----
def _v4(algorithm, key, terminator, access_key, *, region, service, method, path, query, headers, body, stamp):
    date = stamp[:8]
    lower = {k.lower(): ' '.join(str(v).split()) for k, v in headers.items()}
    names = sorted(lower)
    canonical = '\n'.join([method, encode(path or '/', '/'), query_string(query), ''.join(f'{k}:{lower[k]}\n' for k in names),
                           ';'.join(names), sha256_hex(body)])
    scope = f'{date}/{region}/{service}/{terminator}'
    string_to_sign = f'{algorithm}\n{stamp}\n{scope}\n{sha256_hex(canonical)}'
    for part in (date, region, service, terminator):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f'{algorithm} Credential={access_key}/{scope}, SignedHeaders={";".join(names)}, Signature={signature}'


def sigv4(access_key, secret_key, **kw):
    """AWS Signature Version 4 Authorization header; headers must include host and x-amz-date, all are signed."""
    return _v4('AWS4-HMAC-SHA256', 'AWS4' + secret_key, 'aws4_request', access_key, **kw)


def volc(access_key, secret_key, **kw):
    """Volcengine Authorization header; headers must include host and x-date, all are signed."""
    return _v4('HMAC-SHA256', secret_key, 'request', access_key, **kw)
