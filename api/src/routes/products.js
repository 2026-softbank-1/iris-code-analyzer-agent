import { listProducts, findProduct } from '../repositories/products.js';
import { asyncRoute, HttpError } from '../errors.js';
import { uuidSchema } from '../validation.js';
export function registerProducts(app) {
  app.get('/api/products', asyncRoute(async (req, res) => res.json(await listProducts())));
  app.get('/api/products/:id', asyncRoute(async (req, res) => {
    const product = await findProduct(uuidSchema.parse(req.params.id));
    if (!product) throw new HttpError(404, 'Product not found');
    res.json(product);
  }));
}
