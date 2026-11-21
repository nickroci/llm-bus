import type { Env } from './types';
import { HttpError, jsonBody, text, UUID } from './http';
import { retainedMessage, status } from './messages';
const REPORT_REASONS: Record<string, readonly (string | null)[]> = {
  received: [null], proceeding: ['started', 'resumed'],
  not_proceeding: ['needs_approval', 'missing_information', 'dependency_blocked', 'out_of_scope', 'declined'],
  completed: [null], failed: ['validation_failed', 'execution_failed', 'permission_denied', 'unavailable'],
};
interface ReportRow {message_id: string; revision: number; status: string; reason: string | null; expected_revision: number}
// Both participants must retain current grants; identities come only from stored messages.
function reportOwner(now: number): string { return `SELECT m.id FROM messages m
  JOIN sessions s ON s.address=m.sender JOIN sessions r ON r.address=m.recipient
  JOIN machines sm ON sm.id=s.machine_id AND sm.revoked_at IS NULL
  JOIN machines rm ON rm.id=r.machine_id AND rm.revoked_at IS NULL
  JOIN repo_grants sg ON sg.machine_id=s.machine_id AND sg.repo_id=m.repo_id
  JOIN repo_grants rg ON rg.machine_id=r.machine_id AND rg.repo_id=m.repo_id
  WHERE m.id=? AND m.received_at IS NOT NULL AND ${retainedMessage(now)}
  AND COALESCE(m.report_expires_at,strftime('%Y-%m-%dT%H:%M:%fZ',m.sent_at,'+30 days'))>'${new Date(now).toISOString()}'`; }
function reportResult(id: string, row: ReportRow | null): Response {
  return Response.json({message_id:id, revision:row?.revision ?? 0, status:row?.status ?? null, reason:row?.reason ?? null});
}
export async function readReport(url: URL, env: Env, machine: string, now = Date.now()): Promise<Response> {
  const id = text(url.searchParams.get('message_id'), UUID);
  // Before receipt, an authorized sender still sees the revision-zero snapshot.
  const query = reportOwner(now).replace('SELECT m.id', 'SELECT p.*')
    .replace('WHERE m.id', 'LEFT JOIN message_reports p ON p.message_id=m.id WHERE m.id')
    .replace('AND m.received_at IS NOT NULL', '') + ' AND s.machine_id=?';
  const row = await env.DB.prepare(query).bind(id,machine).first<ReportRow>();
  if (!row) throw new HttpError(403, 'Message not allowed');
  return reportResult(id,row);
}
export async function batchRead(request: Request, env: Env, machine: string, kind: 'status' | 'reports'): Promise<Response> {
  const now = Date.now();
  const data = await jsonBody(request);
  const field = kind === 'status' ? 'ids' : 'message_ids';
  const ids = data[field];
  if (Object.keys(data).length !== 1 || !Array.isArray(ids) || ids.length < 1 || ids.length > 100) throw new HttpError(400, 'Invalid batch fields');
  const validated = ids.map(id => text(id, UUID));
  if (new Set(validated).size !== validated.length) throw new HttpError(400, 'Duplicate batch IDs');
  const results = [];
  for (const id of validated) {
    const url = new URL(request.url);
    url.searchParams.set(kind === 'status' ? 'id' : 'message_id', id);
    try {
      // Single-item readers retain primary SQL grant checks for every item.
      const response = await (kind === 'status' ? status(url, env, machine, now) : readReport(url, env, machine, now));
      results.push({id, value: await response.json(), error: null});
    } catch (error) {
      results.push({id, value: null, error: {code: error instanceof HttpError && error.status === 403 ? 'not_allowed' : 'unavailable'}});
    }
  }
  return Response.json({results});
}
export async function submitReport(request: Request, env: Env, machine: string): Promise<Response> {
  const now = Date.now();
  const data = await jsonBody(request);
  if (Object.keys(data).sort().join(',') !== 'expected_revision,message_id,reason,status') throw new HttpError(400, 'Invalid report fields');
  const id = text(data.message_id, UUID);
  const expected = data.expected_revision;
  const status = data.status;
  const reason = data.reason;
  if (typeof expected !== 'number' || !Number.isSafeInteger(expected) || expected < 0 || expected >= Number.MAX_SAFE_INTEGER ||
      typeof status !== 'string' || !Object.hasOwn(REPORT_REASONS, status) ||
      (reason !== null && typeof reason !== 'string') || !REPORT_REASONS[status].includes(reason)) throw new HttpError(400, 'Invalid report');
  const owner = reportOwner(now) + ' AND r.machine_id=?';
  if (!await env.DB.prepare(owner).bind(id,machine).first()) throw new HttpError(403, 'Message not allowed');
  // One atomic SQL compare-and-set handles first reports and competing updates.
  const accepted = await env.DB.prepare(`INSERT INTO message_reports(message_id,revision,status,reason,expected_revision)
    SELECT ?,?+1,?,?,? WHERE EXISTS (${owner}) AND (?=0 OR EXISTS (SELECT 1 FROM message_reports WHERE message_id=?))
    ON CONFLICT(message_id) DO UPDATE SET revision=message_reports.revision+1,
      status=excluded.status,reason=excluded.reason,expected_revision=excluded.expected_revision
    WHERE message_reports.revision=? AND message_reports.status NOT IN ('completed','failed')
      AND (?<>'received' OR message_reports.status='received') AND EXISTS (${owner})
    RETURNING message_id,revision,status,reason,expected_revision`)
    .bind(id,expected,status,reason,expected,id,machine,expected,id,expected,status,id,machine).first<ReportRow>();
  if (accepted) return reportResult(id,accepted);
  const retryQuery = owner.replace('SELECT m.id', 'SELECT p.*')
    .replace('WHERE m.id', 'LEFT JOIN message_reports p ON p.message_id=m.id WHERE m.id');
  const row = await env.DB.prepare(retryQuery).bind(id,machine).first<ReportRow>();
  if (!row) throw new HttpError(403, 'Message not allowed');
  if (!row || row.expected_revision !== expected || row.status !== status || row.reason !== reason) throw new HttpError(409, 'Report revision conflict');
  return reportResult(id,row);
}
