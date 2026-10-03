import './styles.css';
import { api } from './api.js';
import { addItem, checkoutItems } from './cart.js';
import { element } from './format.js';
import { renderProducts } from './views/products.js';
import { renderCart } from './views/cart.js';
import { renderOrders } from './views/orders.js';
const ui = Object.fromEntries(['user', 'connection', 'products', 'cart', 'checkout', 'message', 'orders'].map((id) => [id, document.getElementById(id)]));
let cart = new Map();
let submitting = false;
function updateCart() {
  renderCart(ui.cart, cart, (id) => { cart.delete(id); updateCart(); });
  ui.checkout.disabled = submitting || !cart.size || !ui.user.value;
}
async function refresh() {
  const [products, orders] = await Promise.all([api.products(), api.orders(ui.user.value)]);
  renderProducts(ui.products, products, (product) => { cart = addItem(cart, product); updateCart(); });
  renderOrders(ui.orders, orders);
  return orders;
}
ui.checkout.addEventListener('click', async () => {
  submitting = true; updateCart(); ui.message.textContent = 'Submitting your order…';
  try {
    const order = await api.checkout({ userId: ui.user.value, items: checkoutItems(cart) });
    cart = new Map();
    ui.message.textContent = `Order ${order.id.slice(0, 8)} queued. Waiting for the worker…`;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const current = await api.order(order.id);
      await refresh();
      if (current.status !== 'pending') {
        ui.message.textContent = `Order ${current.status}${current.failureReason ? ': ' + current.failureReason : '.'}`;
        break;
      }
      if (attempt === 19) ui.message.textContent = 'Order is still pending. Check recent orders for updates.';
    }
  } catch (error) { ui.message.textContent = error.message; }
  finally { submitting = false; updateCart(); }
});
ui.user.addEventListener('change', () => refresh().catch((error) => { ui.message.textContent = error.message; }));
async function start() {
  const users = await api.users();
  ui.user.replaceChildren(...users.map((user) => { const option = element('option', user.displayName); option.value = user.id; return option; }));
  await refresh(); updateCart(); ui.connection.textContent = 'API connected';
}
start().catch((error) => { ui.connection.textContent = 'Connection failed'; ui.message.textContent = error.message; });
