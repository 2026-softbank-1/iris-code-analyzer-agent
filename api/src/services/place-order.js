import { randomUUID } from 'node:crypto';
import { transaction, pool } from '../db.js';
import { config } from '../config.js';
import { enqueueOrder } from '../queue.js';
import { HttpError } from '../errors.js';
import { log } from '../logger.js';

export async function placeOrder(input) {
  const id = randomUUID();
  const jobId = randomUUID();
  const totalCents = await transaction(async (client) => {
    const user = await client.query('SELECT id FROM users WHERE id = $1 AND active = TRUE', [input.userId]);
    if (!user.rowCount) throw new HttpError(404, 'User not found');
    const prices = [];
    for (const item of input.items) {
      const result = await client.query('SELECT price_cents, stock FROM products WHERE id = $1 AND active = TRUE', [item.productId]);
      if (!result.rowCount) throw new HttpError(404, 'Product not found');
      if (result.rows[0].stock < item.quantity) throw new HttpError(409, 'Insufficient stock');
      prices.push({ ...item, price: result.rows[0].price_cents });
    }
    const total = prices.reduce((sum, item) => sum + item.price * item.quantity, 0);
    await client.query('INSERT INTO orders (id, user_id, total_cents) VALUES ($1, $2, $3)', [id, input.userId, total]);
    for (const item of prices) {
      await client.query('INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents) VALUES ($1, $2, $3, $4)', [id, item.productId, item.quantity, item.price]);
    }
    await client.query('INSERT INTO jobs (id, order_id, queue_name) VALUES ($1, $2, $3)', [jobId, id, config.queueName]);
    return total;
  });
  let queueAccepted = false;
  try {
    await enqueueOrder(id, jobId);
    queueAccepted = true;
    await pool.query('UPDATE jobs SET dispatched_at = NOW() WHERE id = $1', [jobId]);
  } catch (error) {
    log('queue_dispatch_deferred', { orderId: id, jobId, message: error.message });
  }
  return { id, status: 'pending', totalCents, jobId, queueAccepted };
}
