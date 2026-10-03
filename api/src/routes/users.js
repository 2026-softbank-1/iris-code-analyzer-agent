import { listUsers } from '../repositories/users.js';
import { asyncRoute } from '../errors.js';
export function registerUsers(app) {
  app.get('/api/users', asyncRoute(async (req, res) => res.json(await listUsers())));
}
