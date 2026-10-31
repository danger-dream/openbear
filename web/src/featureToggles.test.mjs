import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyFeatureSettings, featureEnabledIn, featurePageListed, featurePageState, featureSettingsUnavailable,
  featureToggles, resetFeatureTogglesForTest,
} from './featureToggles.js';

test('only an explicit false turns a page off', () => {
  assert.equal(featureEnabledIn({'features.servers': false}, 'servers'), false);
  assert.equal(featureEnabledIn({'features.servers': true}, 'servers'), true);
  // Older controller: no such setting. Undecided controller: null.
  assert.equal(featureEnabledIn({}, 'servers'), true);
  assert.equal(featureEnabledIn({'features.servers': null}, 'servers'), true);
  assert.equal(featureEnabledIn(undefined, 'instances'), true);
});

test('pages stay unlisted and pending until settings arrive', () => {
  resetFeatureTogglesForTest();
  assert.equal(featurePageState('servers'), 'pending');
  assert.equal(featurePageListed('servers'), false);
  assert.equal(featurePageListed('memory'), true);
  assert.equal(featurePageState('memory'), 'on');
  applyFeatureSettings({'features.instances': false, 'features.servers': true});
  assert.equal(featurePageState('instances'), 'off');
  assert.equal(featurePageListed('instances'), false);
  assert.equal(featurePageState('servers'), 'on');
  assert.equal(featurePageListed('servers'), true);
  // An older controller after the fact: everything back on.
  applyFeatureSettings({'ui.showTurnStats': true});
  assert.deepEqual({...featureToggles}, {known: true, instances: true, servers: true});
});

test('unreadable settings keep every page reachable but never override known values', () => {
  resetFeatureTogglesForTest();
  featureSettingsUnavailable();
  assert.equal(featurePageState('instances'), 'on');
  applyFeatureSettings({'features.instances': false});
  featureSettingsUnavailable();
  assert.equal(featurePageState('instances'), 'off');
});
