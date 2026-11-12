import assert from 'node:assert/strict';
import test from 'node:test';
import { applyFeatureSettings, resetFeatureTogglesForTest } from './featureToggles.js';
import { notesSelectionPopoverEnabled, notesSettings, setNotesSelectionPopover } from './notesSettings.js';

test('notesSettings defaults both notesEnabled and selectionPopover to true and syncs immediately', () => {
  resetFeatureTogglesForTest();
  assert.equal(notesSettings.enabled, true);
  assert.equal(notesSettings.selectionPopover, true);
  assert.equal(notesSelectionPopoverEnabled(), true);

  applyFeatureSettings({});
  assert.equal(notesSettings.enabled, true);
  assert.equal(notesSettings.selectionPopover, true);

  setNotesSelectionPopover(false);
  assert.equal(notesSettings.selectionPopover, false);
  assert.equal(notesSelectionPopoverEnabled(), false);

  applyFeatureSettings({ 'ui.notesEnabled': true, 'ui.notesSelectionPopover': true });
  assert.equal(notesSelectionPopoverEnabled(), true);

  // Turning off notes master switch suppresses selection popover even if sub-switch stays true.
  applyFeatureSettings({ 'ui.notesEnabled': false, 'ui.notesSelectionPopover': true });
  assert.equal(notesSettings.enabled, false);
  assert.equal(notesSelectionPopoverEnabled(), false);
});
