import { Miniflare } from 'miniflare';
import { readFileSync } from 'node:fs';
import { beforeEach, beforeAll, afterAll, vi } from 'vitest';
import worker from '../src/index';
import { tokenHash } from '../src/auth';
import type { Env } from '../src/types';
const migration = ['0001_bus.sql', '0002_reports.sql', '0003_reply_to.sql', '0004_coordination.sql', '0005_session_labels.sql', '0006_coordination_availability.sql', '0007_coordination_approvals.sql', '0008_message_retention.sql', '0009_retained_reply_identity.sql'].map(name => readFileSync(new URL(`../migrations/${name}`, import.meta.url), 'utf8')).join(';');

const miniflare = new Miniflare({modules: true, scriptPath: 'dist/index.js', compatibilityDate: '2026-08-01', d1Databases: ['DB'], queueProducers: {DELIVERY: 'delivery'}});
export const bindings = {} as Env;
const sendJob = vi.fn(async (_id: string) => {});
const sendBatch = vi.fn(async (_batch: unknown) => {});
beforeAll(async () => { Object.assign(bindings, {DB: await miniflare.getD1Database('DB'), DELIVERY: {send: sendJob, sendBatch} as unknown as Queue<string>}); });
afterAll(async () => { await miniflare.dispose(); });
const tokens = {a: 'a'.repeat(43), b: 'b'.repeat(43), c: 'c'.repeat(43)};
export async function request(machine: keyof typeof tokens, path: string, data?: object) {
  return worker.fetch(new Request(`https://bus.test/v1/${path}`, {
    method: data ? 'POST' : 'GET',
    headers: {Authorization: `Bearer ${tokens[machine]}`, 'Content-Type': 'application/json'},
    body: data ? JSON.stringify(data) : undefined,
  }), bindings);
}
export async function register(machine: keyof typeof tokens, repo = 'xyz', native = `codex:${machine}`) {
  return request(machine, 'sessions', {repo_id: repo, native_address: native, last_seen: new Date().toISOString(), ended: false});
}
export async function send(id = crypto.randomUUID()) {
  const message = {id, sender: 'codex:a', recipient: 'b/xyz/codex:b', repo_id: 'xyz', body: 'hello'};
  return {message, response: await request('a', 'messages', message)};
}
beforeEach(async () => {
  await bindings.DB.exec('DROP TABLE IF EXISTS message_reports; DROP TABLE IF EXISTS purged_message_ids; DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS sessions; DROP TABLE IF EXISTS repo_grants; DROP TABLE IF EXISTS machines;');
  for (const statement of migration.split(';').filter(s => s.trim())) await bindings.DB.prepare(statement).run();
  for (const machine of ['a', 'b', 'c'] as const) {
    await bindings.DB.prepare('INSERT INTO machines(id,token_hash) VALUES (?,?)').bind(machine, await tokenHash(tokens[machine])).run();
    await bindings.DB.prepare('INSERT INTO repo_grants VALUES (?,?)').bind(machine, machine === 'c' ? 'other' : 'xyz').run();
  }
  await register('a'); await register('b');
});
