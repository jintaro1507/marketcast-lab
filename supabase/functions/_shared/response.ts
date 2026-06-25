import { getAllowedOrigin, makeCorsHeaders } from './cors.ts';

function buildCorsHeaders(origin: string | null): Record<string, string> {
  if (!origin) return {};
  const allowed = getAllowedOrigin(origin);
  if (!allowed) return {};
  return makeCorsHeaders(allowed);
}

export function jsonOk(data: unknown, origin: string | null = null): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: {
      ...buildCorsHeaders(origin),
      'Content-Type': 'application/json',
    },
  });
}

export function jsonError(
  status: number,
  message: string,
  origin: string | null = null,
): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: {
      ...buildCorsHeaders(origin),
      'Content-Type': 'application/json',
    },
  });
}
