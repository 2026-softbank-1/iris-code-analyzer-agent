import { currency, element } from '../format.js';
import { totalCents } from '../cart.js';
export function renderCart(container, cart, onRemove) {
  if (!cart.size) { container.replaceChildren(element('p', 'Your bag is empty.', 'muted')); return; }
  const rows = [...cart.values()].map(({ product, quantity }) => {
    const row = element('div', undefined, 'cart-row');
    row.append(element('span', `${product.name} × ${quantity}`), element('strong', currency(product.priceCents * quantity)));
    const remove = element('button', 'Remove', 'secondary');
    remove.addEventListener('click', () => onRemove(product.id));
    row.append(remove);
    return row;
  });
  container.replaceChildren(...rows, element('p', `Total ${currency(totalCents(cart))}`, 'total'));
}
