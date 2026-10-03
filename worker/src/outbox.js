import { Queue } from 'bullmq';
import { pool } from './db.js';
import { redis } from './redis.js';
import { config } from './config.js';
import { log } from './logger.js';
export const dispatchQueue = new Queue(config.queueName, { connection: redis });
let scanning = false;
export async function dispatchOutbox() {
  if (scanning) return;
  scanning = true;
  try {
    const jobs = (await pool.query("SELECT id, order_id FROM jobs WHERE status = 'queued' AND dispatched_at IS NULL AND queue_name = $1 ORDER BY created_at LIMIT 100", [config.queueName])).rows;
    for (const job of jobs) {
      await dispatchQueue.add('fulfill-order', { orderId: job.order_id, databaseJobId: job.id }, {
        jobId: job.id, attempts: 5, backoff: { type: 'exponential', delay: 1000 },
        removeOnComplete: { age: 86400, count: 1000 }, removeOnFail: { age: 604800 },
      });
      await pool.query('UPDATE jobs SET dispatched_at = NOW() WHERE id = $1', [job.id]);
    }
  } catch (error) { log('outbox_retry', { message: error.message }); }
  finally { scanning = false; }
}
