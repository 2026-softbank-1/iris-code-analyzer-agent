import { pool } from '../db.js';
export async function findJob(id) {
  return (await pool.query('SELECT id, order_id AS "orderId", queue_name AS "queueName", status, attempts, last_error AS "lastError", dispatched_at AS "dispatchedAt", completed_at AS "completedAt" FROM jobs WHERE id = $1', [id])).rows[0];
}
