"""OpenBear version numbers, shared by the controller and the node.

X.Y.Z is a release; X.Y.Z-rc.N and X.Y.Z-beta.N are pre-releases; X.Y.Z-dev.N is a local
development build and is never published. For the same X.Y.Z: release > rc > beta > dev,
then N. Standard semver orders the labels alphabetically (dev before beta, beta before rc),
so every comparison goes through this module instead.
"""
from __future__ import annotations

import re
from typing import NamedTuple

VERSION_RE = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(dev|beta|rc)\.([1-9]\d*))?")
STAGES = ('dev', 'beta', 'rc', 'release')


class Version(NamedTuple):
    major: int
    minor: int
    patch: int
    stage: str
    number: int

    def __str__(self):
        base = f'{self.major}.{self.minor}.{self.patch}'
        return base if self.stage == 'release' else f'{base}-{self.stage}.{self.number}'

    @property
    def key(self):
        return self.major, self.minor, self.patch, STAGES.index(self.stage), self.number


def parse(value) -> Version | None:
    """The version in value ("1.2.0", "v1.2.0-beta.1", ...), or None when it is not one."""
    match = VERSION_RE.fullmatch(str(value or '').strip())
    if not match:
        return None
    major, minor, patch, stage, number = match.groups()
    return Version(int(major), int(minor), int(patch), stage or 'release', int(number or 0))


def valid(value) -> bool:
    return parse(value) is not None


def compare(a, b) -> int:
    """-1, 0 or 1; ValueError when either is not a version."""
    left, right = parse(a), parse(b)
    if left is None or right is None:
        raise ValueError(f'not a version: {a if left is None else b!r}')
    return (left.key > right.key) - (left.key < right.key)


def newer(candidate, current) -> bool:
    """candidate is later than current. An unknown current is older than any version; an unknown candidate is never newer."""
    left, right = parse(candidate), parse(current)
    return left is not None and (right is None or left.key > right.key)


def is_dev(value) -> bool:
    version = parse(value)
    return version is not None and version.stage == 'dev'


def publishable(value) -> bool:
    """beta, rc or release: what may go to GitHub."""
    version = parse(value)
    return version is not None and version.stage != 'dev'


def offered(value, prerelease: bool = False) -> bool:
    """Whether a published version is offered to a user: releases always, beta and rc only to those who opted in."""
    version = parse(value)
    return version is not None and (version.stage == 'release' or (prerelease and version.stage in ('beta', 'rc')))


def pep440(value) -> str:
    """value in the normalized PEP 440 form uv writes to uv.lock: 1.2.0-beta.1 -> 1.2.0b1, 1.2.0-dev.3 -> 1.2.0.dev3."""
    version = parse(value)
    if version is None:
        raise ValueError(f'not a version: {value!r}')
    base = f'{version.major}.{version.minor}.{version.patch}'
    return base + {'release': '', 'rc': f'rc{version.number}', 'beta': f'b{version.number}', 'dev': f'.dev{version.number}'}[version.stage]


def next_dev(value) -> str:
    """The development version after publishing value: the next minor's dev.1, whatever value's stage."""
    version = parse(value)
    if version is None:
        raise ValueError(f'not a version: {value!r}')
    return str(Version(version.major, version.minor + 1, 0, 'dev', 1))


def bump_dev(value) -> str:
    """value with its dev number one higher; a version that is not a development one stays as it is."""
    version = parse(value)
    if version is None:
        raise ValueError(f'not a version: {value!r}')
    return str(version._replace(number=version.number + 1)) if version.stage == 'dev' else str(version)
