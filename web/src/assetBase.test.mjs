import test from 'node:test';
import assert from 'node:assert/strict';
import {assetBase, assetUrl} from './assetBase.js';

test('only an injected gateway instance prefix replaces the root asset base', () => {
  assert.equal(assetBase({}), '/');
  assert.equal(assetUrl('assets/pwa/manifest-dark.webmanifest', {}), '/assets/pwa/manifest-dark.webmanifest');
  assert.equal(assetBase({__OPENBEAR_ASSET_BASE__: '/_i/family-a_2/'}), '/_i/family-a_2/');
  assert.equal(assetUrl('assets/brand/x.png', {__OPENBEAR_ASSET_BASE__: '/_i/a/'}), '/_i/a/assets/brand/x.png');
  for (const value of ['[[asset-base]]', '', '/', '/_i/a', '/_i//', '/_i/a/b/', '//evil.example/', 'https://evil.example/_i/a/', '/_i/a b/', 7, null]) {
    assert.equal(assetBase({__OPENBEAR_ASSET_BASE__: value}), '/', String(value));
  }
});
