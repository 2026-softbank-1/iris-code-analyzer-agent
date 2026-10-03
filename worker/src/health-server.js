import http from 'node:http';
import { pool } from './db.js';
import { redis } from './redis.js';
import { config } from './config.js';
export function startHealthServer() {
  return http.createServer(async (req, res) => {
    res.setHeader('content-type', 'application/json');
    if (req.url !== '/health') { res.writeHead(404); res.end('{"error":"Not found"}'); return; }
    try {
      await Promise.all([pool.query('SELECT 1'), redis.ping()]);
      res.end(JSON.stringify({ status: 'ready', service: 'worker' }));
    } catch {
      res.writeHead(503); res.end('{"status":"unready"}');
    }
  }).listen(config.healthPort, '0.0.0.0');
}
