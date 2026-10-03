import { ZodError } from 'zod';
import { log } from '../logger.js';
export function errorHandler(error, req, res, next) {
  if (res.headersSent) return next(error);
  const status = error instanceof ZodError ? 400 : error.status || 500;
  if (status >= 500) log('request_error', { path: req.path, message: error.message });
  res.status(status).json({ error: status >= 500 ? 'Internal server error' : error.message });
}
