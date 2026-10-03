import { currency, element } from '../format.js';
export function renderProducts(container, products, onAdd) {
  container.replaceChildren(...products.map((product) => {
    const card = element('article', undefined, 'product');
    card.append(element('span', product.sku, 'eyebrow'), element('h3', product.name), element('p', product.description));
    card.append(element('strong', currency(product.priceCents)), element('p', `${product.stock} available`, 'muted'));
    const button = element('button', product.stock ? 'Add to bag' : 'Sold out');
    button.disabled = product.stock === 0;
    button.addEventListener('click', () => onAdd(product));
    card.append(button);
    return card;
  }));
}
