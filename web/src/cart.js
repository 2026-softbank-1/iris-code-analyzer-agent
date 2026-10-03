export function addItem(cart, product) {
  const next = new Map(cart);
  const item = next.get(product.id) || { product, quantity: 0 };
  if (item.quantity < Math.min(product.stock, 20)) next.set(product.id, { product, quantity: item.quantity + 1 });
  return next;
}
export function totalCents(cart) {
  return [...cart.values()].reduce((sum, item) => sum + item.product.priceCents * item.quantity, 0);
}
export function checkoutItems(cart) {
  return [...cart.values()].map(({ product, quantity }) => ({ productId: product.id, quantity }));
}
