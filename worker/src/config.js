function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}
export const config = {
  nodeEnv: process.env.NODE_ENV || 'development',
  databaseUrl: required('DATABASE_URL'),
  redisUrl: required('REDIS_URL'),
  queueName: process.env.ORDER_QUEUE_NAME || 'order-processing',
  concurrency: Number(process.env.WORKER_CONCURRENCY || 2),
  healthPort: Number(process.env.HEALTH_PORT || 3001),
  outboxPollMs: Number(process.env.OUTBOX_POLL_MS || 5000),
  logLevel: process.env.LOG_LEVEL || 'info',
};
