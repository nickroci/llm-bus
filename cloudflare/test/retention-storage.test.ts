import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';
import { bindings, request, send } from './relay-fixture';
import { dispatch } from '../src/messages';

const migration = readFileSync(new URL('../migrations/0008_message_retention.sql', import.meta.url), 'utf8');

async function undoRetentionSchema() {
  await bindings.DB.batch([
    'DROP INDEX messages_retention',
    'ALTER TABLE messages DROP COLUMN retention_at',
    'ALTER TABLE messages DROP COLUMN report_expires_at', 'DROP TABLE purged_message_ids',
  ].map(sql => bindings.DB.prepare(sql)));
}
async function migrate() {
  return bindings.DB.batch(migration.split(';').filter(sql => sql.trim()).map(sql => bindings.DB.prepare(sql)));
}

it('preserves legacy deadlines while normalizing offset ages and keeps new deadlines stable on retries', async () => {
  const {message} = await send();
  await undoRetentionSchema();
  await bindings.DB.prepare("UPDATE messages SET sent_at='2026-11-01T02:00:00+02:00' WHERE id=?").bind(message.id).run();
  await migrate();
  expect(await bindings.DB.prepare('SELECT retention_at,report_expires_at FROM messages WHERE id=?').bind(message.id).first())
    .toEqual({retention_at:'2026-11-01T00:00:00.000Z',report_expires_at:null});
  const legacy = await (await request('a',`status?id=${message.id}`)).json() as {report_expires_at:string;retention_expires_at:string};
  expect(legacy.report_expires_at).toBe('2026-12-01T00:00:00.000Z');
  expect(legacy.retention_expires_at).toBe('2026-11-15T00:00:00.000Z');
  const fresh = await send();
  const original = await fresh.response.json() as {report_expires_at:string;retention_expires_at:string};
  const retry = await (await request('a','messages',fresh.message)).json();
  expect(retry).toEqual(original);
  expect(original.report_expires_at).toBe(original.retention_expires_at);
  const sentAt = await bindings.DB.prepare('SELECT sent_at FROM messages WHERE id=?').bind(fresh.message.id).first<string>('sent_at');
  expect(Date.parse(original.report_expires_at) - Date.parse(sentAt!)).toBe(1_209_600_000);
  const plan = await bindings.DB.prepare('EXPLAIN QUERY PLAN SELECT id FROM messages WHERE retention_at<=? ORDER BY retention_at,sequence LIMIT 100').bind('2026-11-10T00:00:00.000Z').all<{detail:string}>();
  expect(plan.results.some(row=>row.detail.includes('messages_retention'))).toBe(true);
});

it.each(['invalid private content','2026-02-30T00:00:00Z','2026-01-01'])('rejects malformed legacy age without partial migration: %s', async age => {
  const {message} = await send();
  await undoRetentionSchema();
  await bindings.DB.prepare('UPDATE messages SET sent_at=? WHERE id=?').bind(age,message.id).run();
  await expect(migrate()).rejects.toThrow();
  const columns = await bindings.DB.prepare('PRAGMA table_info(messages)').all<{name:string}>();
  expect(columns.results.some(row=>row.name==='retention_at')).toBe(false);
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS count FROM messages').first('count')).toBe(1);
});

it('rejects purged UUID reuse atomically and cannot dispatch a delayed job into a missing row', async () => {
  const {message} = await send();
  await bindings.DB.batch([
    bindings.DB.prepare('INSERT INTO purged_message_ids VALUES (?)').bind(message.id),
    bindings.DB.prepare('DELETE FROM messages WHERE id=?').bind(message.id),
  ]);
  const results = await Promise.all([request('a','messages',message),request('a','messages',message)]);
  expect(results.map(row=>row.status)).toEqual([403,403]);
  await dispatch(bindings,message.id);
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS count FROM messages').first('count')).toBe(0);
});
