"""Every module in this package that does not start with an underscore defines adapters."""
from __future__ import annotations

import importlib
import pkgutil

from app.ddns.providers.base import REGISTRY, Provider, ProviderError, register

TIER_ORDER = {'A': 0, 'B': 1, 'C': 2}

for _module in pkgutil.iter_modules(__path__):
    if not _module.name.startswith('_') and _module.name != 'base':
        importlib.import_module(f'{__name__}.{_module.name}')


def get(kind):
    return REGISTRY.get(kind)


def catalog():
    return [cls.public() for cls in sorted(REGISTRY.values(), key=lambda c: (TIER_ORDER.get(c.tier, 9), c.name.lower()))]


__all__ = ['REGISTRY', 'Provider', 'ProviderError', 'register', 'get', 'catalog']
