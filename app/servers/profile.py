"""Owner-entered server facts: grouping, billing and traffic allowance."""
from __future__ import annotations

import datetime as dt
import math
import re

from app.instances.contracts import InstanceError

TRAFFIC_TYPES = ('max', 'sum', 'up', 'down', 'min')
# billing: monthly on the billing date's day; fixed: monthly on resetDay;
# metered: paid per GB; unlimited: usage only. All periods are monthly.
TRAFFIC_MODES = ('billing', 'fixed', 'metered', 'unlimited')
_ISO = re.compile(r'[A-Z]{2}')


def _text(value, limit, error):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise InstanceError(error)
    return value.strip()


def _date(value):
    if value in (None, ''):
        return ''
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise InstanceError('invalid_server_date') from None


def _number(value, low, high, error, integer=False):
    if value in (None, ''):
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise InstanceError(error)
    return int(value) if integer else round(float(value), 4)


def clean(data, prior=None):
    """Merge a PATCH-style update; unknown keys are rejected."""
    prior = prior or {}
    if not isinstance(data, dict) or set(data) - {'alias', 'group', 'tags', 'provider', 'providerUrl', 'remark', 'region', 'sort', 'billing', 'traffic', 'access'}:
        raise InstanceError('invalid_server_profile')
    out = dict(prior)
    for key, limit in (('alias', 60), ('group', 40), ('provider', 60), ('remark', 2000)):
        if key in data:
            out[key] = _text(data[key], limit, f'invalid_server_{key}')
    if 'tags' in data:
        out['tags'] = _tags(data['tags'])
    if 'providerUrl' in data:
        url = _text(data['providerUrl'], 300, 'invalid_server_provider_url')
        if url and not re.fullmatch(r'https?://[^\s/$.?#][^\s]*', url, re.I):
            raise InstanceError('invalid_server_provider_url')
        out['providerUrl'] = url
    if 'region' in data:
        region = _text(data['region'], 2, 'invalid_server_region').upper()
        if region and not _ISO.fullmatch(region):
            raise InstanceError('invalid_server_region')
        out['region'] = region
    if 'sort' in data:
        out['sort'] = _number(data['sort'], -100000, 100000, 'invalid_server_sort', integer=True)
    if 'access' in data:
        a = data['access'] or {}
        if not isinstance(a, dict) or set(a) - {'primary', 'backup'}:
            raise InstanceError('invalid_server_access')
        out['access'] = {k: _text(a.get(k), 40, 'invalid_server_access') for k in ('primary', 'backup')}
    if 'billing' in data:
        b, old = data['billing'] or {}, prior.get('billing') or {}
        if not isinstance(b, dict) or set(b) - {'price', 'currency', 'cycle', 'startedAt', 'expiresAt', 'autoRenew', 'billingDay'}:
            raise InstanceError('invalid_server_billing')
        merged = {**old, **b}
        cycle = _number(merged.get('cycle'), -1, 3650, 'invalid_billing_cycle', integer=True)
        expires = _date(merged.get('expiresAt'))
        started = _date(merged.get('startedAt'))
        # Not given: one cycle before the expiry, when both are known.
        if not started and expires and cycle > 0:
            started = sub_cycle(dt.date.fromisoformat(expires), cycle).isoformat()
        out['billing'] = {'price': _number(merged.get('price'), 0, 1e9, 'invalid_billing_price'),
                          'currency': _text(merged.get('currency'), 8, 'invalid_billing_currency'),
                          'cycle': cycle, 'startedAt': started,
                          'expiresAt': expires, 'autoRenew': merged.get('autoRenew') is True}
        if expires:
            out['billing']['billingDay'] = billing_day(merged.get('billingDay'), dt.date.fromisoformat(expires))
    if 'traffic' in data:
        t, old = data['traffic'] or {}, prior.get('traffic') or {}
        if not isinstance(t, dict) or set(t) - {'mode', 'limit', 'type', 'resetDay', 'price'}:
            raise InstanceError('invalid_server_traffic')
        merged = {**old, **t}
        mode = traffic_mode(merged)
        if mode not in TRAFFIC_MODES:
            raise InstanceError('invalid_traffic_mode')
        # Both directions together unless chosen; traffic set up before without a type counted the larger one.
        kind = merged.get('type') or ('max' if old else 'sum')
        if kind not in TRAFFIC_TYPES:
            raise InstanceError('invalid_traffic_type')
        reset = _number(merged.get('resetDay'), 0, 31, 'invalid_traffic_reset_day', integer=True)
        if mode == 'fixed' and not reset:
            raise InstanceError('invalid_traffic_reset_day')
        limited = mode in ('billing', 'fixed')
        out['traffic'] = {'mode': mode, 'type': kind,
                          'limit': _number(merged.get('limit'), 0, 2**60, 'invalid_traffic_limit', integer=True) if limited else 0,
                          'resetDay': reset if mode == 'fixed' else 0,
                          'price': _number(merged.get('price'), 0, 1e6, 'invalid_traffic_price') if mode == 'metered' else 0}
    return out


def _tags(value):
    """Labels besides the one group (独服, xxx 专用): up to 10, 20 characters each, in order, once."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 10:
        raise InstanceError('invalid_server_tags')
    out = []
    for tag in value:
        if not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 20:
            raise InstanceError('invalid_server_tags')
        if tag.strip() not in out:
            out.append(tag.strip())
    return out


def _months(cycle):
    return next((m for lo, hi, m in ((27, 32, 1), (87, 95, 3), (175, 185, 6), (360, 370, 12), (720, 750, 24),
                                     (1080, 1150, 36), (1800, 1850, 60)) if lo <= cycle <= hi), 0)


def sub_cycle(day, cycle):
    """One billing cycle before day: add_cycle backwards."""
    months = _months(cycle)
    if not months:
        return day - dt.timedelta(days=cycle)
    month = day.month - 1 - months
    year, month = day.year + month // 12, month % 12 + 1
    return dt.date(year, month, min(day.day, _last_day(year, month)))


def _last_day(year, month):
    return (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)).day


def billing_day(day, expires):
    """The day of month the bill falls on: kept while the expiry is that day, or the last day of a
    shorter month (31 stays 31 through 02-28); any other expiry the owner sets starts its own."""
    if type(day) is int and 1 <= day <= 31 and expires.day == min(day, _last_day(expires.year, expires.month)):
        return day
    return expires.day


def add_cycle(day, cycle, anchor=None):
    """Next expiry after one billing cycle. Month-like cycles keep the day of
    month (anchor, the billing day, when given), other values add plain days."""
    months = _months(cycle)
    if not months:
        return day + dt.timedelta(days=cycle)
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return dt.date(year, month, min(anchor or day.day, _last_day(year, month)))


def renew(billing, expires, today):
    """The expiry an automatic renewal moves expires to: cycles from it until today is covered."""
    renewed = expires
    while renewed < today:
        renewed = add_cycle(renewed, billing['cycle'], billing.get('billingDay') or expires.day)
    return renewed


def traffic_mode(traffic):
    """Profiles saved before modes existed: a reset day meant a fixed date."""
    traffic = traffic or {}
    return traffic.get('mode') or ('fixed' if traffic.get('resetDay') else 'billing')


def period_start(reset_day, today):
    """Start of the current monthly traffic period for a reset day (1..31)."""
    def on(year, month):
        return dt.date(year, month, min(reset_day, _last_day(year, month)))
    this = on(today.year, today.month)
    if this <= today:
        return this
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return on(year, month)


def traffic_used(kind, rx, tx):
    return {'sum': rx + tx, 'up': tx, 'down': rx, 'min': min(rx, tx)}.get(kind, max(rx, tx))
