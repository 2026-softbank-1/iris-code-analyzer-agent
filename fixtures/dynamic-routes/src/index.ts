import express from 'express';
const app = express();
const route = process.env.DYNAMIC_PATH;
app.get(route, (_req, res) => res.send('ok'));
app.listen(Number(process.env.PORT));
