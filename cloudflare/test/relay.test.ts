import { Miniflare } from 'miniflare';
import { readFileSync } from 'node:fs';
import { beforeEach, beforeAll, afterAll, describe, expect, it, vi } from 'vitest';
import worker, { dispatch, repair, tokenHash, type Env } from '../src/index';
const migration = readFileSync(new URL('../migrations/0001_bus.sql', import.meta.url), 'utf8');

const miniflare = new Miniflare({modules: true, scriptPath: 'dist/index.js', compatibilityDate: '2026-08-01', d1Databases: ['DB'], queueProducers: {DELIVERY: 'delivery'}});
let bindings: Env;
const sendJob = vi.fn(async (_id: string) => {});
const sendBatch = vi.fn(async (_batch: unknown) => {});
beforeAll(async () => { bindings = {DB: await miniflare.getD1Database('DB'), DELIVERY: {send: sendJob, sendBatch} as unknown as Queue<string>}; });
afterAll(async () => { await miniflare.dispose(); });
const tokens = {a: 'a'.repeat(43), b: 'b'.repeat(43), c: 'c'.repeat(43)};
async function request(machine: keyof typeof tokens, path: string, data?: object) {
  return worker.fetch(new Request(`https://bus.test/v1/${path}`, {
    method: data ? 'POST' : 'GET',
    headers: {Authorization: `Bearer ${tokens[machine]}`, 'Content-Type': 'application/json'},
    body: data ? JSON.stringify(data) : undefined,
  }), bindings);
}
async function register(machine: keyof typeof tokens, repo = 'xyz', native = `codex:${machine}`) {
  return request(machine, 'sessions', {repo_id: repo, native_address: native, last_seen: new Date().toISOString(), ended: false});
}
async function send(id = crypto.randomUUID()) {
  const message = {id, sender: 'codex:a', recipient: 'b/xyz/codex:b', repo_id: 'xyz', body: 'hello'};
  return {message, response: await request('a', 'messages', message)};
}
beforeEach(async () => {
  await bindings.DB.exec('DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS sessions; DROP TABLE IF EXISTS repo_grants; DROP TABLE IF EXISTS machines;');
  for (const statement of migration.split(';').filter(s => s.trim())) await bindings.DB.prepare(statement).run();
  for (const machine of ['a', 'b', 'c'] as const) {
    await bindings.DB.prepare('INSERT INTO machines(id,token_hash) VALUES (?,?)').bind(machine, await tokenHash(tokens[machine])).run();
    await bindings.DB.prepare('INSERT INTO repo_grants VALUES (?,?)').bind(machine, machine === 'c' ? 'other' : 'xyz').run();
  }
  await register('a'); await register('b');
});

describe('machine and repository boundary', () => {
  it('rejects invalid credentials and unauthorized registration/discovery', async () => {
    const invalid = await worker.fetch(new Request('https://bus.test/v1/sessions?repo_id=xyz'), bindings);
    expect(invalid.status).toBe(401);
    expect((await register('c')).status).toBe(403);
    expect((await request('c', 'sessions?repo_id=xyz')).status).toBe(403);
    expect((await request('a', 'sessions?repo_id=xyz')).status).toBe(200);
    expect((await request('a', 'inbox?repo_id=other')).status).toBe(403);
  });
  it('rejects cross-repo recipients and spoofed sender sessions', async () => {
    await register('c', 'other');
    const {message} = await send();
    expect((await request('a', 'messages', {...message, id: crypto.randomUUID(), recipient: 'c/other/codex:c'})).status).toBe(403);
    expect((await request('a', 'messages', {...message, id: crypto.randomUUID(), sender: 'codex:b'})).status).toBe(403);
    const count = await bindings.DB.prepare('SELECT count(*) AS n FROM messages').first<{n:number}>();
    expect(count?.n).toBe(1);
  });
  it('revokes queued delivery and acknowledgements when a grant or machine is revoked', async () => {
    const {message} = await send(); await dispatch(bindings, message.id);
    expect((await request('a', 'receipts', {id: message.id})).status).toBe(403);
    await bindings.DB.prepare("DELETE FROM repo_grants WHERE machine_id='a'").run();
    expect(await (await request('b', 'inbox?repo_id=xyz')).json()).toEqual([]);
    expect((await request('b', 'receipts', {id: message.id})).status).toBe(403);
    await bindings.DB.prepare("UPDATE machines SET revoked_at='now' WHERE id='b'").run();
    expect((await request('b', 'inbox?repo_id=xyz')).status).toBe(401);
  });
});

describe('durability and receipts', () => {
  it('deduplicates identical uploads, rejects conflicting UUID reuse, and keeps reads pending', async () => {
    const {message, response} = await send(); expect(response.status).toBe(202);
    expect((await request('a', 'messages', message)).status).toBe(200);
    expect((await request('a', 'messages', {...message, body: 'different'})).status).toBe(409);
    await dispatch(bindings, message.id); await dispatch(bindings, message.id);
    for (let n=0; n<2; n++) {
      const inbox = await (await request('b', 'inbox?repo_id=xyz')).json() as unknown[];
      expect(inbox).toHaveLength(1);
    }
    expect((await request('b', 'ack', {id: message.id})).status).toBe(403);
    expect((await request('b', 'receipts', {id: message.id})).status).toBe(200);
    expect((await request('b', 'receipts', {id: message.id})).status).toBe(200);
    expect(await (await request('b', 'inbox?repo_id=xyz')).json()).toEqual([]);
    expect((await request('b', 'ack', {id: message.id})).status).toBe(200);
    expect(await (await request('a', `status?id=${message.id}`)).json()).toEqual({id: message.id, status: 'acknowledged'});
  });
  it('finds earlier messages when queue delivery is reversed and a new scan starts', async () => {
    const first = await send(); const second = await send();
    await dispatch(bindings, second.message.id);
    const later = await (await request('b', 'inbox?repo_id=xyz&after=0')).json() as {id:string}[];
    expect(later.map(m=>m.id)).toEqual([second.message.id]);
    await request('b', 'receipts', {id: second.message.id});
    await dispatch(bindings, first.message.id);
    const earlier = await (await request('b', 'inbox?repo_id=xyz&after=0')).json() as {id:string}[];
    expect(earlier.map(m=>m.id)).toEqual([first.message.id]);
  });
  it('retains dispatch records for scheduled repair and reports expiry', async () => {
    const {message} = await send();
    await repair(bindings);
    expect(await bindings.DB.prepare('SELECT id FROM messages WHERE ready=0').first()).not.toBeNull();
    await bindings.DB.prepare("UPDATE messages SET sent_at='2020-01-01T00:00:00.000Z' WHERE id=?").bind(message.id).run();
    expect(await (await request('a', `status?id=${message.id}`)).json()).toEqual({id: message.id, status: 'expired'});
    expect(await (await request('b', 'inbox?repo_id=xyz')).json()).toEqual([]);
  });
});

it('accepts valid escaped payloads and durable sends after sessions end', async () => {
  await bindings.DB.prepare('UPDATE sessions SET ended=1').run();
  const message = {id:crypto.randomUUID(), sender:'codex:a',recipient:'b/xyz/codex:b',repo_id:'xyz',body:'\u0001'.repeat(65536)};
  expect((await request('a','messages',message)).status).toBe(202);
  const unicode = {...message,id:crypto.randomUUID(),body:'界'.repeat(16000)};
  expect((await request('a','messages',unicode)).status).toBe(202);
  expect((await request('a','messages',{...message,id:crypto.randomUUID(),body:'界'.repeat(22000)})).status).toBe(400);
});
