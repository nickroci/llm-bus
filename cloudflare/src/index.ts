/** Per-machine authenticated, same-repository durable relay. */
export interface Env {
  DB: D1Database;
  DELIVERY: Queue<string>;
}
interface Session {
  address: string;
  machine_id: string;
  repo_id: string;
  native_address: string;
  last_seen: string;
  ended: number;
}
interface MessageRow {
  sequence: number;
  id: string;
  sender: string;
  recipient: string;
  repo_id: string;
  body: string;
  sent_at: string;
  received_at: string | null;
  acknowledged_at: string | null;
}
const BATCH = 100;
const MAX_BODY_BYTES = 64 * 1024;
const MAX_REQUEST_BYTES = MAX_BODY_BYTES * 6 + 4096;
const ID = /^[A-Za-z0-9_-]{1,128}$/;
const NATIVE = /^(claude|codex):[A-Za-z0-9_-]{1,128}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
// Keep authorization reads on D1's primary. No replica sessions or positive grant cache.
const ACTIVE_GRANT = `EXISTS (SELECT 1 FROM repo_grants g JOIN machines m ON m.id=g.machine_id
  WHERE g.machine_id=? AND g.repo_id=? AND m.revoked_at IS NULL)`;
const LIVE_MESSAGE = `m.sent_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days')`;
class HttpError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}
function text(value: unknown, pattern?: RegExp): string {
  if (typeof value !== 'string' || (pattern && !pattern.test(value))) {
    throw new HttpError(400, 'Invalid field');
  }
  return value;
}
async function jsonBody(request: Request): Promise<Record<string, unknown>> {
  const reader = request.body?.getReader();
  if (!reader) throw new HttpError(400, 'Expected JSON object');
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_REQUEST_BYTES) {
      await reader.cancel();
      throw new HttpError(413, 'Request too large');
    }
    chunks.push(value);
  }
  const data = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  let parsed: unknown;
  try { parsed = JSON.parse(new TextDecoder('utf-8', {fatal: true, ignoreBOM: false}).decode(data)); }
  catch { throw new HttpError(400, 'Invalid JSON'); }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new HttpError(400, 'Expected JSON object');
  }
  return parsed as Record<string, unknown>;
}
export async function tokenHash(token: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  return [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2, '0')).join('');
}
async function authenticate(request: Request, env: Env): Promise<string> {
  const match = /^Bearer ([A-Za-z0-9_-]{43,128})$/.exec(request.headers.get('Authorization') ?? '');
  if (!match) throw new HttpError(401, 'Invalid machine credential');
  const row = await env.DB.prepare('SELECT id FROM machines WHERE token_hash=? AND revoked_at IS NULL')
    .bind(await tokenHash(match[1])).first<{id: string}>();
  if (!row) throw new HttpError(401, 'Invalid machine credential');
  return row.id;
}
async function grant(env: Env, machine: string, repo: string): Promise<void> {
  const row = await env.DB.prepare(`SELECT 1 AS allowed WHERE ${ACTIVE_GRANT}`)
    .bind(machine, repo).first();
  if (!row) throw new HttpError(403, 'Repository not allowed');
}
function qualified(machine: string, repo: string, native: string): string {
  return `${machine}/${repo}/${native}`;
}
async function register(request: Request, env: Env, machine: string): Promise<Response> {
  const data = await jsonBody(request);
  const repo = text(data.repo_id, ID);
  const native = text(data.native_address, NATIVE);
  const seen = text(data.last_seen);
  const timestamp = Date.parse(seen);
  if (!Number.isFinite(timestamp) || timestamp > Date.now() + 60_000 || typeof data.ended !== 'boolean') {
    throw new HttpError(400, 'Invalid session activity');
  }
  const address = qualified(machine, repo, native);
  const result = await env.DB.prepare(`INSERT INTO sessions
    (address,machine_id,repo_id,native_address,last_seen,ended)
    SELECT ?,?,?,?,?,? WHERE ${ACTIVE_GRANT}
    ON CONFLICT(address) DO UPDATE SET last_seen=excluded.last_seen, ended=excluded.ended
    WHERE excluded.last_seen >= sessions.last_seen`)
    .bind(address, machine, repo, native, new Date(timestamp).toISOString(), Number(data.ended), machine, repo).run();
  // Stale updates are harmless, but a missing grant is always an error.
  if (!result.meta.changes) await grant(env, machine, repo);
  return Response.json({address});
}
async function sessions(url: URL, env: Env, machine: string): Promise<Response> {
  const repo = text(url.searchParams.get('repo_id'), ID);
  await grant(env, machine, repo);
  const rows = await env.DB.prepare(`SELECT s.* FROM sessions s JOIN machines d ON d.id=s.machine_id
    JOIN repo_grants g ON g.machine_id=s.machine_id AND g.repo_id=s.repo_id
    WHERE s.repo_id=? AND s.machine_id<>? AND s.ended=0 AND d.revoked_at IS NULL
    AND s.last_seen > strftime('%Y-%m-%dT%H:%M:%fZ','now','-24 hours')
    AND ${ACTIVE_GRANT} ORDER BY s.address LIMIT 1000`)
    .bind(repo, machine, machine, repo).all<Session>();
  return Response.json(rows.results);
}
async function send(request: Request, env: Env, machine: string): Promise<Response> {
  const data = await jsonBody(request);
  const id = text(data.id, UUID);
  const repo = text(data.repo_id, ID);
  const sender = qualified(machine, repo, text(data.sender, NATIVE));
  const recipient = text(data.recipient);
  const body = text(data.body);
  if (!body.trim() || new TextEncoder().encode(body).length > MAX_BODY_BYTES) {
    throw new HttpError(400, 'Body must contain 1–65536 UTF-8 bytes');
  }
  await grant(env, machine, repo);
  const prior = await env.DB.prepare('SELECT * FROM messages WHERE id=?').bind(id).first<MessageRow>();
  if (prior) {
    if (prior.sender !== sender || prior.recipient !== recipient || prior.repo_id !== repo || prior.body !== body) {
      throw new HttpError(409, 'Message ID already used');
    }
    return Response.json({id, status: messageStatus(prior)});
  }
  const result = await env.DB.prepare(`INSERT INTO messages(id,sender,recipient,repo_id,body)
    SELECT ?,s.address,r.address,?,? FROM sessions s JOIN sessions r ON r.address=?
    JOIN machines rm ON rm.id=r.machine_id AND rm.revoked_at IS NULL
    JOIN repo_grants rg ON rg.machine_id=r.machine_id AND rg.repo_id=r.repo_id
    WHERE s.address=? AND s.repo_id=? AND r.repo_id=? AND r.machine_id<>?
    AND ${ACTIVE_GRANT}
    ON CONFLICT(id) DO NOTHING`)
    .bind(id, repo, body, recipient, sender, repo, repo, machine, machine, repo).run();
  if (!result.meta.changes) {
    const raced = await env.DB.prepare('SELECT * FROM messages WHERE id=?').bind(id).first<MessageRow>();
    if (raced && raced.sender === sender && raced.recipient === recipient && raced.body === body && raced.repo_id === repo) {
      return Response.json({id, status: messageStatus(raced)});
    }
    throw new HttpError(raced ? 409 : 403, 'Recipient or sender not allowed');
  }
  // A failed enqueue cannot undo the durable acceptance. The scheduled scan repairs it.
  try { await env.DELIVERY.send(id); } catch { /* durable pending row is the outbox */ }
  return Response.json({id, status: 'accepted_remote'}, {status: 202});
}
function messageStatus(row: MessageRow): string {
  if (row.acknowledged_at) return 'acknowledged';
  if (row.received_at) return 'delivered_local';
  if (Date.parse(row.sent_at) <= Date.now() - 30 * 86400_000) return 'expired';
  return 'accepted_remote';
}
async function inbox(url: URL, env: Env, machine: string): Promise<Response> {
  const repo = text(url.searchParams.get('repo_id'), ID);
  const after = Number(url.searchParams.get('after') ?? 0);
  if (!Number.isSafeInteger(after) || after < 0) throw new HttpError(400, 'Invalid cursor');
  await grant(env, machine, repo);
  const rows = await env.DB.prepare(`SELECT m.* FROM messages m
    JOIN sessions r ON r.address=m.recipient JOIN sessions s ON s.address=m.sender
    JOIN machines sm ON sm.id=s.machine_id AND sm.revoked_at IS NULL
    JOIN repo_grants sg ON sg.machine_id=s.machine_id AND sg.repo_id=m.repo_id
    WHERE r.machine_id=? AND m.repo_id=? AND m.sequence>? AND m.ready=1
    AND m.received_at IS NULL AND ${LIVE_MESSAGE} AND ${ACTIVE_GRANT}
    ORDER BY m.sequence LIMIT ?`)
    .bind(machine, repo, after, machine, repo, BATCH).all<MessageRow>();
  const page: MessageRow[] = [];
  let bytes = 2;
  for (const row of rows.results) {
    const size = new TextEncoder().encode(JSON.stringify(row)).length + 1;
    if (bytes + size > 1024 * 1024 && page.length) break;
    bytes += size;
    page.push(row);
  }
  return Response.json(page);
}
async function receipt(request: Request, env: Env, machine: string, acknowledge: boolean): Promise<Response> {
  const data = await jsonBody(request);
  const id = text(data.id, UUID);
  const column = acknowledge ? 'acknowledged_at' : 'received_at';
  const result = await env.DB.prepare(`UPDATE messages AS m SET ${column}=COALESCE(${column},strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    WHERE m.id=? AND EXISTS (SELECT 1 FROM sessions r JOIN repo_grants g
      ON g.machine_id=r.machine_id AND g.repo_id=m.repo_id JOIN machines d ON d.id=r.machine_id
      WHERE r.address=m.recipient AND r.machine_id=? AND d.revoked_at IS NULL)
    AND EXISTS (SELECT 1 FROM sessions s JOIN repo_grants g
      ON g.machine_id=s.machine_id AND g.repo_id=m.repo_id JOIN machines d ON d.id=s.machine_id
      WHERE s.address=m.sender AND d.revoked_at IS NULL)
    AND m.ready=1 AND (${acknowledge ? 'm.received_at IS NOT NULL' : LIVE_MESSAGE})`)
    .bind(id, machine).run();
  if (!result.meta.changes) throw new HttpError(403, 'Message not allowed');
  return Response.json({id, status: acknowledge ? 'acknowledged' : 'delivered_local'});
}
async function status(url: URL, env: Env, machine: string): Promise<Response> {
  const id = text(url.searchParams.get('id'), UUID);
  const row = await env.DB.prepare(`SELECT m.* FROM messages m JOIN sessions s ON s.address=m.sender
    WHERE m.id=? AND s.machine_id=? AND ${ACTIVE_GRANT.replace('g.repo_id=?', 'g.repo_id=m.repo_id')}`)
    .bind(id, machine, machine).first<MessageRow>();
  if (!row) throw new HttpError(403, 'Message not allowed');
  return Response.json({id, status: messageStatus(row)});
}
export async function dispatch(env: Env, id: string): Promise<void> {
  // Mailbox authorization is checked again at fetch time, including token revocation.
  await env.DB.prepare('UPDATE messages SET ready=1 WHERE id=? AND ready=0').bind(id).run();
}
export async function repair(env: Env): Promise<void> {
  const pending = await env.DB.prepare(`SELECT id FROM messages WHERE ready=0
    AND sent_at > strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days') ORDER BY sequence LIMIT ?`)
    .bind(BATCH).all<{id: string}>();
  if (pending.results.length) await env.DELIVERY.sendBatch(pending.results.map(row => ({body: row.id})));
}
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      const machine = await authenticate(request, env);
      const url = new URL(request.url);
      const route = `${request.method} ${url.pathname}`;
      switch (route) {
        case 'POST /v1/sessions': return await register(request, env, machine);
        case 'GET /v1/sessions': return await sessions(url, env, machine);
        case 'POST /v1/messages': return await send(request, env, machine);
        case 'GET /v1/inbox': return await inbox(url, env, machine);
        case 'POST /v1/receipts': return await receipt(request, env, machine, false);
        case 'POST /v1/ack': return await receipt(request, env, machine, true);
        case 'GET /v1/status': return await status(url, env, machine);
        default: throw new HttpError(404, 'Unknown endpoint');
      }
    } catch (error) {
      if (error instanceof HttpError) return Response.json({error: error.message}, {status: error.status});
      // Do not emit request headers, message bodies, tokens, or SQL errors.
      return Response.json({error: 'Relay temporarily unavailable'}, {status: 503});
    }
  },
  async queue(batch: MessageBatch<string>, env: Env): Promise<void> {
    for (const message of batch.messages) {
      try { text(message.body, UUID); await dispatch(env, message.body); message.ack(); }
      catch { message.retry(); }
    }
  },
  async scheduled(_event: ScheduledController, env: Env): Promise<void> { await repair(env); }
};
