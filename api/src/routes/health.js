import { pool } from '../db.js';
import { redis } from '../queue.js';
import { asyncRoute } from '../errors.js';
export function registerHealth(app) {
  app.get('/health', (req, res) => res.json({ status: 'ok', service: 'api' }));
  app.get('/ready', asyncRoute(async (req, res) => {
    await Promise.all([pool.query('SELECT 1'), redis.ping()]);
    res.json({ status: 'ready', dependencies: ['postgres', 'redis'] });
  }));
}
