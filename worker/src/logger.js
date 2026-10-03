export function log(event, details = {}) {
  console.log(JSON.stringify({ time: new Date().toISOString(), event, ...details }));
}
