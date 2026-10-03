import { Worker } from 'bullmq';
import { config } from './config.js';
import { redis } from './redis.js';
import { pool } from './db.js';
import { processOrder } from './process-order.js';
import { recordFailure } from './record-failure.js';
import { dispatchOutbox, dispatchQueue } from './outbox.js';
import { startHealthServer } from './health-server.js';
import { log } from './logger.js';
const worker = new Worker(config.queueName, processOrder, { connection: redis, concurrency: config.concurrency });
worker.on('failed', async (job, error) => {
  log('job_failed', { jobId: job?.id, message: error.message });
  try { await recordFailure(job, error); }
  catch (dbError) { log('job_error_record_failed', { message: dbError.message }); }
});
worker.on('error', (error) => log('worker_error', { message: error.message }));
const health = startHealthServer();
const timer = setInterval(dispatchOutbox, config.outboxPollMs);
await dispatchOutbox();
log('worker_started', { queue: config.queueName, concurrency: config.concurrency, healthPort: config.healthPort });
async function shutdown(signal) {
  log('shutdown', { signal });
  clearInterval(timer);
  const deadline = setTimeout(() => process.exit(1), 10000).unref();
  health.close();
  await Promise.allSettled([worker.close(), dispatchQueue.close()]);
  await pool.end();
  redis.disconnect();
  clearTimeout(deadline);
  process.exit(0);
}
process.once('SIGTERM', () => shutdown('SIGTERM'));
process.once('SIGINT', () => shutdown('SIGINT'));
