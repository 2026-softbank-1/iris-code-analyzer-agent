const app = require('express')();
app.get('/health', (req, res) => res.send('OK'));
app.get('/catalog', (req, res) => res.json([{ id: 'demo', name: 'Demo item' }]));
app.listen(3000, '0.0.0.0');
