function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}
export const config = {
  nodeEnv: process.env.NODE_ENV || 'development',
  port: Number(process.env.PORT || 3000),
  databaseUrl: required('DATABASE_URL'),
  redisUrl: required('REDIS_URL'),
  queueName: process.env.ORDER_QUEUE_NAME || 'order-processing',
  logLevel: process.env.LOG_LEVEL || 'info',
};
