import { findJob } from '../repositories/jobs.js';
import { asyncRoute, HttpError } from '../errors.js';
import { uuidSchema } from '../validation.js';
export function registerJobs(app) {
  app.get('/api/jobs/:id', asyncRoute(async (req, res) => {
    const job = await findJob(uuidSchema.parse(req.params.id));
    if (!job) throw new HttpError(404, 'Job not found');
    res.json(job);
  }));
}
