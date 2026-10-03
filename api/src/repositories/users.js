import { pool } from '../db.js';
export async function listUsers() {
  return (await pool.query('SELECT id, email, display_name AS "displayName" FROM users WHERE active = TRUE ORDER BY display_name')).rows;
}
