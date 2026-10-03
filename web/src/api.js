const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';
export async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
export const api = {
  users: () => request('/users'),
  products: () => request('/products'),
  orders: (userId) => request(`/orders?userId=${encodeURIComponent(userId)}`),
  order: (id) => request(`/orders/${encodeURIComponent(id)}`),
  checkout: (input) => request('/orders', { method: 'POST', body: JSON.stringify(input) }),
};
