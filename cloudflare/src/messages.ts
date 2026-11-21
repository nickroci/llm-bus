import type { Env, MessageRow } from './types';
import { HttpError, jsonBody, text, ID, NATIVE, UUID, MAX_BODY_BYTES } from './http';
import { ACTIVE_GRANT, grant } from './auth';
import { qualified } from './sessions';
export const BATCH = 100;
export const LIVE_MESSAGE = `m.sent_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days')`;
const APPROVAL_OPERATIONS = ['approve-commit','approve-push','approve-merge','approve-merge-admin'];
const BRANCH = /^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$/;
const SHA = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const TICKET = /^(?:[^\p{C}\p{Z}]| )+$/u;
const EXPIRY = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/;
function direction(value: unknown): {metadata: string; operation: string} {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpError(400, 'Invalid coordination');
  const data = value as Record<string, unknown>;
  if (Object.keys(data).sort().join(',') !== 'generation,grant_id,operation,project_id,version'
    || data.version !== 1 || typeof data.operation !== 'string'
    || !['assign','reprioritize','clarify','pause','cancel','review',...APPROVAL_OPERATIONS].includes(data.operation)) {
    throw new HttpError(400, 'Invalid coordination');
  }
  return {operation: data.operation, metadata: JSON.stringify({version:1,project_id:text(data.project_id,UUID),generation:text(data.generation,UUID),grant_id:text(data.grant_id,UUID),operation:data.operation})};
}
function expiry(value: unknown): string {
  const match = typeof value === 'string' ? EXPIRY.exec(value) : null;
  if (!match) throw new HttpError(400, 'Invalid approval');
  const [year, month, day, hour, minute, second, offsetHour = '0', offsetMinute = '0'] = match.slice(1).map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day
    || hour > 23 || minute > 59 || second > 59 || Number(offsetHour) > 23 || Number(offsetMinute) > 59) throw new HttpError(400, 'Invalid approval');
  return value as string;
}
// Same shape rules as the client's Approval.validate, except that a record_id is local-only and never crosses machines.
function approval(value: unknown, operation: string): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpError(400, 'Invalid approval');
  const data = value as Record<string, unknown>;
  if (Object.keys(data).some(key => !['ticket','branch','base','expires_at','pr','head_sha'].includes(key))) throw new HttpError(400, 'Invalid approval');
  const ticket = text(data.ticket);
  if ([...ticket].length > 128 || !TICKET.test(ticket)) throw new HttpError(400, 'Invalid approval');
  const merge = operation === 'approve-merge' || operation === 'approve-merge-admin';
  if ((data.pr !== undefined) !== merge || (merge && (!Number.isSafeInteger(data.pr) || (data.pr as number) < 1))) throw new HttpError(400, 'Invalid approval');
  if ((data.head_sha === undefined) !== (operation === 'approve-commit')) throw new HttpError(400, 'Invalid approval');
  return JSON.stringify({ticket,branch:text(data.branch,BRANCH),base:text(data.base,BRANCH),expires_at:expiry(data.expires_at),
    ...(data.pr === undefined ? {} : {pr:data.pr}), ...(data.head_sha === undefined ? {} : {head_sha:text(data.head_sha,SHA)})});
}
function availability(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpError(400, 'Invalid availability');
  const data = value as Record<string, unknown>;
  if (Object.keys(data).sort().join(',') !== 'generation,grant_id,kind,project_id,version'
    || data.version !== 1 || data.kind !== 'joined') throw new HttpError(400, 'Invalid availability');
  return JSON.stringify({version:1,project_id:text(data.project_id,UUID),generation:text(data.generation,UUID),grant_id:text(data.grant_id,UUID),kind:'joined'});
}
type MessageClass = 'ordinary' | 'coordination' | 'availability';
export async function send(request: Request, env: Env, machine: string, kind: MessageClass = 'ordinary'): Promise<Response> {
  const data = await jsonBody(request);
  if ((kind !== 'coordination' && ('coordination' in data || 'approval' in data)) || (kind !== 'availability' && 'availability' in data)) throw new HttpError(400, 'Use matching message endpoint');
  if (kind !== 'ordinary' && Object.keys(data).some(key => !['id','sender','recipient','repo_id','body','reply_to',kind,...(kind === 'coordination' ? ['approval'] : [])].includes(key))) throw new HttpError(400, 'Invalid typed envelope');
  const typed = kind === 'coordination' ? direction(data.coordination) : null;
  const metadata = typed?.metadata ?? null;
  const gate = typed !== null && APPROVAL_OPERATIONS.includes(typed.operation);
  if (typed !== null && gate !== ('approval' in data)) throw new HttpError(400, 'Invalid approval');
  const evidence = gate ? approval(data.approval, typed.operation) : null;
  const event = kind === 'availability' ? availability(data.availability) : null;
  const id = text(data.id, UUID);
  const repo = text(data.repo_id, ID);
  const sender = qualified(machine, repo, text(data.sender, NATIVE));
  const recipient = text(data.recipient);
  const body = text(data.body);
  const reply = data.reply_to === undefined ? null : text(data.reply_to, UUID);
  if (!body.trim() || new TextEncoder().encode(body).length > MAX_BODY_BYTES) {
    throw new HttpError(400, 'Body must contain 1–65536 UTF-8 bytes');
  }
  await grant(env, machine, repo);
  const prior = await env.DB.prepare('SELECT * FROM messages WHERE id=?').bind(id).first<MessageRow>();
  if (prior) {
    if (prior.sender !== sender || prior.recipient !== recipient || prior.repo_id !== repo || prior.body !== body || prior.reply_to !== reply || prior.coordination !== metadata || prior.availability !== event || prior.approval !== evidence) {
      throw new HttpError(409, 'Message ID already used');
    }
    return Response.json(transportResult(prior));
  }
  const acceptedAt = new Date();
  const sentAt = acceptedAt.toISOString();
  const reportDeadline = new Date(acceptedAt.getTime() + 14 * 86400_000).toISOString();
  const result = await env.DB.prepare(`INSERT INTO messages(id,sender,recipient,repo_id,body,reply_to,coordination,availability,approval,sent_at,retention_at,report_expires_at)
    SELECT ?,s.address,r.address,?,?,?,?,?,?,?,?,? FROM sessions s JOIN sessions r ON r.address=?
    JOIN machines rm ON rm.id=r.machine_id AND rm.revoked_at IS NULL
    JOIN repo_grants rg ON rg.machine_id=r.machine_id AND rg.repo_id=r.repo_id
    WHERE s.address=? AND s.repo_id=? AND r.repo_id=? AND r.machine_id<>?
    AND ${ACTIVE_GRANT}
    AND NOT EXISTS (SELECT 1 FROM purged_message_ids WHERE id=?)
    AND (? IS NULL OR EXISTS (SELECT 1 FROM messages p WHERE p.id=? AND p.repo_id=?
      AND ((p.sender=s.address AND p.recipient=r.address) OR (p.sender=r.address AND p.recipient=s.address))))
    ON CONFLICT(id) DO NOTHING`)
    .bind(id, repo, body, reply, metadata, event, evidence, sentAt, sentAt, reportDeadline, recipient, sender, repo, repo, machine, machine, repo, id, reply, reply, repo).run();
  if (!result.meta.changes) {
    const raced = await env.DB.prepare('SELECT * FROM messages WHERE id=?').bind(id).first<MessageRow>();
    if (raced && raced.sender === sender && raced.recipient === recipient && raced.body === body && raced.repo_id === repo && raced.reply_to === reply && raced.coordination === metadata && raced.availability === event && raced.approval === evidence) {
      return Response.json(transportResult(raced));
    }
    throw new HttpError(raced ? 409 : 403, 'Recipient or sender not allowed');
  }
  // A failed enqueue cannot undo the durable acceptance. The scheduled scan repairs it.
  try { await env.DELIVERY.send(id); } catch { /* durable pending row is the outbox */ }
  const accepted = await env.DB.prepare('SELECT * FROM messages WHERE id=?').bind(id).first<MessageRow>();
  if (!accepted) throw new HttpError(503, 'Message unavailable');
  return Response.json(transportResult(accepted), {status: 202});
}
function transportResult(row: MessageRow): {id: string; status: string; report_expires_at: string; retention_expires_at: string} {
  return {id:row.id,status:messageStatus(row),report_expires_at:row.report_expires_at ?? new Date(Date.parse(row.sent_at) + 30 * 86400_000).toISOString(),
    retention_expires_at:new Date(Date.parse(row.retention_at) + 14 * 86400_000).toISOString()};
}
function messageStatus(row: MessageRow): string {
  if (row.acknowledged_at) return 'acknowledged';
  if (row.received_at) return 'delivered_local';
  if (Date.parse(row.sent_at) <= Date.now() - 30 * 86400_000) return 'expired';
  return 'accepted_remote';
}
export async function inbox(url: URL, env: Env, machine: string, kind: MessageClass = 'ordinary'): Promise<Response> {
  const repo = text(url.searchParams.get('repo_id'), ID);
  const after = Number(url.searchParams.get('after') ?? 0);
  if (!Number.isSafeInteger(after) || after < 0) throw new HttpError(400, 'Invalid cursor');
  await grant(env, machine, repo);
  const rows = await env.DB.prepare(`SELECT m.* FROM messages m
    JOIN sessions r ON r.address=m.recipient JOIN sessions s ON s.address=m.sender
    JOIN machines sm ON sm.id=s.machine_id AND sm.revoked_at IS NULL
    JOIN repo_grants sg ON sg.machine_id=s.machine_id AND sg.repo_id=m.repo_id
    WHERE r.machine_id=? AND m.repo_id=? AND m.sequence>? AND m.ready=1
    AND m.coordination IS ${kind === 'coordination' ? 'NOT NULL' : 'NULL'}
    AND m.availability IS ${kind === 'availability' ? 'NOT NULL' : 'NULL'}
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
  return Response.json(page.map(row => {
    const {coordination: metadata, availability: event, approval: evidence, ...ordinary} = row;
    return {...ordinary, reply_to:row.reply_to ?? undefined, ...(metadata === null ? {} : {coordination:JSON.parse(metadata)}), ...(event === null ? {} : {availability:JSON.parse(event)}), ...(evidence === null ? {} : {approval:JSON.parse(evidence)})};
  }));
}
export async function receipt(request: Request, env: Env, machine: string, acknowledge: boolean): Promise<Response> {
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
export async function status(url: URL, env: Env, machine: string): Promise<Response> {
  const id = text(url.searchParams.get('id'), UUID);
  const row = await env.DB.prepare(`SELECT m.* FROM messages m JOIN sessions s ON s.address=m.sender
    WHERE m.id=? AND s.machine_id=? AND ${ACTIVE_GRANT.replace('g.repo_id=?', 'g.repo_id=m.repo_id')}`)
    .bind(id, machine, machine).first<MessageRow>();
  if (!row) throw new HttpError(403, 'Message not allowed');
  return Response.json(transportResult(row));
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
