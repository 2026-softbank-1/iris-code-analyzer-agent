import Redis from 'ioredis';
import { config } from './config.js';
export const redis = new Redis(config.redisUrl, { maxRetriesPerRequest: null, connectTimeout: 3000 });
redis.on('error', () => {});
