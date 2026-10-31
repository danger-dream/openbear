import pytest

from app import versioning


def test_order_is_release_rc_beta_dev_then_number():
    ordered = ['1.0.0', '1.0.1-dev.1', '1.0.1-dev.9', '1.0.1-beta.1', '1.0.1-beta.2', '1.0.1-rc.1', '1.0.1', '1.1.0-dev.1', '1.1.0', '2.0.0-dev.1']
    assert sorted(reversed(ordered), key=lambda v: versioning.parse(v).key) == ordered
    for earlier, later in zip(ordered, ordered[1:]):
        assert versioning.compare(later, earlier) == 1 and versioning.compare(earlier, later) == -1
        assert versioning.newer(later, earlier) and not versioning.newer(earlier, later)
    # Numbers compare as numbers, not text.
    assert versioning.newer('1.0.1-beta.10', '1.0.1-beta.9') and versioning.newer('1.10.0', '1.9.0')


def test_forms_it_takes_and_refuses():
    assert str(versioning.parse('v1.2.0-beta.3')) == '1.2.0-beta.3' and str(versioning.parse(' 0.13.1 ')) == '0.13.1'
    for bad in ('1.2', '1.2.0.dev.1', '1.2.0-alpha.1', '1.2.0-beta', '1.2.0-beta.0', '01.2.0', '1.2.0-dev.01', '', None, 'node-v1.0.0'):
        assert versioning.parse(bad) is None and not versioning.valid(bad)
    with pytest.raises(ValueError):
        versioning.compare('1.0.0', 'latest')
    # An unknown current build is older than anything; an unknown candidate never wins.
    assert versioning.newer('0.0.1', 'garbage') and not versioning.newer('garbage', '0.0.1')


def test_what_may_be_published_and_who_is_offered_it():
    assert not versioning.publishable('1.2.0-dev.4') and all(versioning.publishable(v) for v in ('1.2.0-beta.1', '1.2.0-rc.1', '1.2.0'))
    assert versioning.is_dev('1.2.0-dev.4') and not versioning.is_dev('1.2.0')
    assert versioning.offered('1.2.0') and not versioning.offered('1.2.0-beta.1') and not versioning.offered('1.2.0-rc.1')
    assert versioning.offered('1.2.0-beta.1', prerelease=True) and versioning.offered('1.2.0-rc.1', prerelease=True)
    assert not versioning.offered('1.2.0-dev.1', prerelease=True)


def test_development_numbers():
    assert versioning.next_dev('1.1.0') == '1.2.0-dev.1'
    assert versioning.next_dev('1.2.0-beta.1') == '1.3.0-dev.1' and versioning.next_dev('v1.2.3-rc.2') == '1.3.0-dev.1'
    assert versioning.bump_dev('1.2.0-dev.1') == '1.2.0-dev.2' and versioning.bump_dev('1.2.0-dev.9') == '1.2.0-dev.10'
    # A build of a version about to be published keeps that version.
    assert versioning.bump_dev('1.2.0-beta.1') == '1.2.0-beta.1' and versioning.bump_dev('1.2.0') == '1.2.0'
