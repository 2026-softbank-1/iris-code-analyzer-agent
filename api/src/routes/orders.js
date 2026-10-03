import { listOrders, findOrder } from '../repositories/orders.js';
import { placeOrder } from '../services/place-order.js';
import { asyncRoute, HttpError } from '../errors.js';
import { orderSchema, uuidSchema } from '../validation.js';
export function registerOrders(app) {
  app.get('/api/orders', asyncRoute(async (req, res) => {
    const userId = req.query.userId ? uuidSchema.parse(req.query.userId) : undefined;
    res.json(await listOrders(userId));
  }));
  app.get('/api/orders/:id', asyncRoute(async (req, res) => {
    const order = await findOrder(uuidSchema.parse(req.params.id));
    if (!order) throw new HttpError(404, 'Order not found');
    res.json(order);
  }));
  app.post('/api/orders', asyncRoute(async (req, res) => {
    const order = await placeOrder(orderSchema.parse(req.body));
    res.status(202).json(order);
  }));
}
