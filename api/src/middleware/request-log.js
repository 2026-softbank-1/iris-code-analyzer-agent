import { log } from '../logger.js';
export function requestLog(req, res, next) {
  const start = Date.now();
  res.on('finish', () => log('request', { method: req.method, path: req.path, status: res.statusCode, durationMs: Date.now() - start }));
  next();
}
