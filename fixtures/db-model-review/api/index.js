const express = require('express');
const { MongoClient } = require('mongodb');
const client = new MongoClient(process.env.MONGO_URI);
const app = express();
app.get('/health', (_req, res) => res.json({ok: true}));
async function main() {
  await client.connect();
  app.listen(3000);
}
main().catch(() => process.exit(1));
