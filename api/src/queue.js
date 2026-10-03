import { Queue } from 'bullmq';
import Redis from 'ioredis';
import { config } from './config.js';
export const redis = new Redis(config.redisUrl, { maxRetriesPerRequest: 1, connectTimeout: 3000 });
redis.on('error', () => {});
export const orderQueue = new Queue(config.queueName, { connection: redis });
export function enqueueOrder(orderId, jobId) {
  return orderQueue.add('fulfill-order', { orderId, databaseJobId: jobId }, {
    jobId, attempts: 5, backoff: { type: 'exponential', delay: 1000 },
    removeOnComplete: { age: 86400, count: 1000 }, removeOnFail: { age: 604800 },
  });
}
