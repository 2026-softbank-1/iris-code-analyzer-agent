import { currency, element } from '../format.js';
export function renderOrders(container, orders) {
  container.replaceChildren(...orders.map((order) => {
    const row = element('div', undefined, 'order-row');
    row.append(element('code', order.id.slice(0, 8)), element('span', currency(order.totalCents)), element('span', order.status, `status ${order.status}`));
    if (order.failureReason) row.append(element('span', order.failureReason));
    return row;
  }));
  if (!orders.length) container.append(element('p', 'No orders yet.', 'muted'));
}
