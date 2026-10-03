import { app } from './app.js';
import { config } from './config.js';
import { pool } from './db.js';
import { orderQueue, redis } from './queue.js';
import { log } from './logger.js';
const server = app.listen(config.port, '0.0.0.0', () => log('api_started', { port: config.port, environment: config.nodeEnv }));
async function shutdown(signal) {
  log('shutdown', { signal });
  const timer = setTimeout(() => process.exit(1), 10000).unref();
  server.close(async () => {
    await Promise.allSettled([orderQueue.close(), pool.end()]);
    redis.disconnect();
    clearTimeout(timer);
    process.exit(0);
  });
}
process.once('SIGTERM', () => shutdown('SIGTERM'));
process.once('SIGINT', () => shutdown('SIGINT'));
