import { transaction } from './db.js';
export async function recordFailure(job, error) {
  if (!job?.data.databaseJobId) return;
  const terminal = job.attemptsMade >= Number(job.opts.attempts || 1) || await job.getState() === 'failed';
  await transaction(async (client) => {
    const order = (await client.query('SELECT status FROM orders WHERE id = $1 FOR UPDATE', [job.data.orderId])).rows[0];
    if (!order || order.status !== 'pending') return;
    await client.query('UPDATE jobs SET last_error = $1, attempts = GREATEST(attempts, $2) WHERE id = $3 AND order_id = $4', [error.message, job.attemptsMade, job.data.databaseJobId, job.data.orderId]);
    if (terminal) {
      await client.query("UPDATE orders SET status = 'rejected', failure_reason = 'Background job failed after retries', updated_at = NOW() WHERE id = $1", [job.data.orderId]);
      await client.query("UPDATE jobs SET status = 'rejected', completed_at = NOW() WHERE id = $1 AND order_id = $2", [job.data.databaseJobId, job.data.orderId]);
    }
  });
}
