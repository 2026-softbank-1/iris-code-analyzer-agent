const redisUrl = process.env.REDIS_URL;
const sessionSecret = process.env["SESSION_SECRET"];
console.log(Boolean(redisUrl && sessionSecret));
