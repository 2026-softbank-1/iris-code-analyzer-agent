import { test } from 'node:test';
import assert from 'node:assert/strict';
import { orderSchema } from '../src/validation.js';
const userId = '11111111-1111-4111-8111-111111111111';
const productId = '22222222-2222-4222-8222-222222222221';
test('accepts a bounded checkout', () => assert.equal(orderSchema.parse({ userId, items: [{ productId, quantity: 2 }] }).items[0].quantity, 2));
test('rejects duplicate products', () => assert.equal(orderSchema.safeParse({ userId, items: [{ productId, quantity: 1 }, { productId, quantity: 2 }] }).success, false));
test('rejects fractional and excessive quantities', () => {
  for (const quantity of [0, 1.5, 21]) assert.equal(orderSchema.safeParse({ userId, items: [{ productId, quantity }] }).success, false);
});
test('rejects invalid identities and empty orders', () => {
  assert.equal(orderSchema.safeParse({ userId: 'invalid', items: [{ productId, quantity: 1 }] }).success, false);
  assert.equal(orderSchema.safeParse({ userId, items: [] }).success, false);
});
