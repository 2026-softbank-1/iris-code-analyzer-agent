import { pool } from '../db.js';
export async function listOrders(userId) {
  const query = 'SELECT id, user_id AS "userId", status, total_cents AS "totalCents", failure_reason AS "failureReason", created_at AS "createdAt" FROM orders';
  return (await pool.query(`${query} ${userId ? 'WHERE user_id = $1' : ''} ORDER BY created_at DESC LIMIT 50`, userId ? [userId] : [])).rows;
}
export async function findOrder(id) {
  const order = (await pool.query('SELECT id, user_id AS "userId", status, total_cents AS "totalCents", failure_reason AS "failureReason", created_at AS "createdAt" FROM orders WHERE id = $1', [id])).rows[0];
  if (!order) return null;
  order.items = (await pool.query('SELECT i.product_id AS "productId", p.name, i.quantity, i.unit_price_cents AS "unitPriceCents" FROM order_items i JOIN products p ON p.id = i.product_id WHERE i.order_id = $1 ORDER BY p.name', [id])).rows;
  order.job = (await pool.query('SELECT id, status, attempts FROM jobs WHERE order_id = $1', [id])).rows[0];
  return order;
}
