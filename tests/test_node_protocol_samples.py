"""The controller side of the shared protocol samples (node/testdata/protocol.md).

The fake node checks every message and call against them while the servers tests run; this
file holds the controller's own constants and the matcher to them, so a change on this side
that the samples do not say fails here too. node/protocol_samples_test.go is the Go side."""
from __future__ import annotations

import base64
import hashlib

import pytest

from app.servers import compat, install, releases
from tests import fake_node
from tests import node_protocol as samples


def test_every_sample_is_read_and_the_go_test_names_the_same_ones():
    names = set(samples.SAMPLES)
    assert {'msg:hello', 'msg:config', 'msg:auth', 'enroll:request', 'call:exec', 'call:update', 'refusal:capability_disabled'} <= names
    # The Go test lists every sample it checks; one added here without a check there fails there.
    go = (samples.PATH.parents[1] / 'protocol_samples_test.go').read_text()
    missing = [name for name in names if f'"{name}"' not in go]
    assert not missing, missing


def test_the_controller_speaks_the_sampled_protocol_range():
    hello = samples.SAMPLES['msg:hello']
    assert compat.PROTOCOL == (hello['protocolMin'], hello['protocol']) == (fake_node.PROTOCOL_MIN, fake_node.PROTOCOL)


def test_the_sampled_node_has_every_feature_the_controller_requires():
    assert set(releases.REQUIRED_FEATURES) <= set(samples.SAMPLES['msg:hello']['features'])


def test_the_install_fingerprint_is_what_the_node_compares():
    shared = samples.SAMPLES['enroll:fingerprint']
    key = base64.b64decode(shared['controllerKey'])
    assert install.fingerprint(shared['controllerKey']) == shared['fingerprint'] == hashlib.sha256(key).hexdigest()


def test_every_method_refused_without_exec_has_a_sample():
    refused = samples.SAMPLES['refusal:capability_disabled']
    assert not [m for m in refused if not samples.method_sample(m)]


@pytest.mark.parametrize(('value', 'pattern', 'loose', 'ok'), [
    ({'a': 1}, {'a': '<int>'}, False, True),
    ({'a': 1.5}, {'a': '<int>'}, False, False),
    ({}, {'a?': '<string>'}, False, True),
    ({'b': 1}, {'a?': '<string>'}, False, False),  # no unknown keys
    ({'a': 'x'}, {'a': 'y'}, False, False),
    ({'a': 'x'}, {'a': 'y'}, True, True),  # loose: types only
    ({'a': 1}, {'a': 'y'}, True, False),
    ([1, 2, 3], ['<each>', '<int>'], False, True),
    ([1, 'x'], ['<each>', '<int>'], False, False),
    ([1], [1, 2], False, False),
    (True, '<int>', False, False),  # a bool is not a number
    ('0' * 64, '<hex64>', False, True),
    ('A' * 64, '<hex64>', False, False),
    ('aGk=', '<base64>', False, True),
    ('not base64!', '<base64>', False, False),
    (None, '<int?>', False, True),
    ({'mode': '0644', 'size': 5}, '<ref:entry>', False, False),  # entry needs name, type and mtime
])
def test_the_matcher_reads_the_notation_as_the_file_says(value, pattern, loose, ok):
    assert (not samples.problems(value, pattern, loose=loose)) is ok
