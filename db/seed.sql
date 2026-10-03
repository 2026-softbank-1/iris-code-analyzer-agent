BEGIN;
INSERT INTO users (id, email, display_name)
VALUES ('11111111-1111-4111-8111-111111111111', 'demo@example.test', 'Demo Shopper');

INSERT INTO products (id, sku, name, description, price_cents, stock) VALUES
  ('22222222-2222-4222-8222-222222222221', 'IRIS-MUG', 'Iris Mug', 'Ceramic mug for long build sessions.', 1800, 40),
  ('22222222-2222-4222-8222-222222222222', 'IRIS-NOTE', 'Field Notebook', 'Grid notebook for ideas and deployment notes.', 900, 80),
  ('22222222-2222-4222-8222-222222222223', 'IRIS-BAG', 'Everyday Tote', 'Canvas bag for the next hackathon.', 2400, 25);
COMMIT;
