const port = process.env.HEALTH_PORT || 3001;
try {
  const response = await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(4000) });
  process.exit(response.ok ? 0 : 1);
} catch { process.exit(1); }
