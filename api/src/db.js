import pg from 'pg';
import { config } from './config.js';
export const pool = new pg.Pool({ connectionString: config.databaseUrl, max: 10, connectionTimeoutMillis: 3000 });
export async function transaction(fn) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const result = await fn(client);
    await client.query('COMMIT');
    return result;
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally { client.release(); }
}
