import { transaction, pool } from './db.js';
import { log } from './logger.js';
export async function processOrder(job) {
  const { orderId, databaseJobId } = job.data;
  await pool.query('UPDATE jobs SET attempts = GREATEST(attempts, $1) WHERE id = $2 AND order_id = $3', [job.attemptsMade + 1, databaseJobId, orderId]);
  const status = await transaction(async (client) => {
    const result = await client.query('SELECT id, status FROM orders WHERE id = $1 FOR UPDATE', [orderId]);
    if (!result.rowCount) throw new Error(`Order ${orderId} not found`);
    const order = result.rows[0];
    if (order.status !== 'pending') return order.status;
    const items = (await client.query('SELECT product_id, quantity FROM order_items WHERE order_id = $1 ORDER BY product_id', [orderId])).rows;
    let rejected = null;
    for (const item of items) {
      const product = (await client.query('SELECT stock, active FROM products WHERE id = $1 FOR UPDATE', [item.product_id])).rows[0];
      if (!product?.active || product.stock < item.quantity) rejected = 'Inventory unavailable';
    }
    if (!rejected) {
      for (const item of items) await client.query('UPDATE products SET stock = stock - $1 WHERE id = $2', [item.quantity, item.product_id]);
    }
    const nextStatus = rejected ? 'rejected' : 'completed';
    await client.query('UPDATE orders SET status = $1, failure_reason = $2, updated_at = NOW() WHERE id = $3', [nextStatus, rejected, orderId]);
    await client.query('UPDATE jobs SET status = $1, last_error = $2, completed_at = NOW() WHERE id = $3 AND order_id = $4', [nextStatus, rejected, databaseJobId, orderId]);
    return nextStatus;
  });
  log('order_processed', { orderId, jobId: databaseJobId, status });
  return { orderId, status };
}
