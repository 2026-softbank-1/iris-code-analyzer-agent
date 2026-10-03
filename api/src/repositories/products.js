import { pool } from '../db.js';
export async function listProducts() {
  return (await pool.query('SELECT id, sku, name, description, price_cents AS "priceCents", stock FROM products WHERE active = TRUE ORDER BY name')).rows;
}
export async function findProduct(id) {
  return (await pool.query('SELECT id, sku, name, description, price_cents AS "priceCents", stock FROM products WHERE id = $1 AND active = TRUE', [id])).rows[0];
}
