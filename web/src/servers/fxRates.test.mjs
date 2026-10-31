import test from 'node:test';
import assert from 'node:assert/strict';
import {parseRates} from './fxRates.js';

test('rates from either source become CNY per unit', () => {
  assert.deepEqual(parseRates({result: 'success', time_last_update_unix: 1792886551, rates: {CNY: 1, USD: 0.14, EUR: 0.125, usd: 3}}),
    {date: '2026-10-25', rates: {CNY: 1, USD: 7.14286, EUR: 8}});
  assert.deepEqual(parseRates({base: 'CNY', date: '2026-10-23', rates: {USD: 0.2}}), {date: '2026-10-23', rates: {USD: 5, CNY: 1}});
  assert.equal(parseRates({result: 'error', rates: {USD: 0.2}}), null);
  assert.equal(parseRates({rates: {}}), null);
});
