export class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
export function asyncRoute(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
}
