import { afterEach, expect, it, vi } from 'vitest';
import worker from '../src/index';
import { BATCH, dispatch, purge } from '../src/messages';
import { bindings, request, send } from './relay-fixture';

const DAY = 86400_000;
const NOW = Date.parse('2026-11-21T12:00:00.000Z');
afterEach(() => vi.restoreAllMocks());
function clock() { return vi.spyOn(Date,'now').mockReturnValue(NOW); }
async function age(id: string, milliseconds: number) {
  await bindings.DB.prepare('UPDATE messages SET sent_at=?,retention_at=? WHERE id=?')
    .bind(new Date(NOW-milliseconds).toISOString(),new Date(NOW-milliseconds).toISOString(),id).run();
}
async function typedRequest(kind: string, machine: string, endpoint: string, data?: object) {
  const prefix = kind === 'ordinary' ? '/v1/' : `/v2/coordination/${kind === 'availability' ? 'availability/' : ''}`;
  return worker.fetch(new Request(`https://bus.test${prefix}${endpoint}`, {
    method:data ? 'POST' : 'GET', headers:{Authorization:`Bearer ${machine.repeat(43)}`,'Content-Type':'application/json'},
    body:data ? JSON.stringify(data) : undefined,
  }),bindings);
}

// Protect every inbox class and late queue work at the exact delivery boundary.
it.each(['ordinary','coordination','availability'])('enforces 48 hours for %s inbox, queue dispatch, and first receipts', async kind => {
  clock();
  const metadata = {version:1,project_id:crypto.randomUUID(),generation:crypto.randomUUID(),grant_id:crypto.randomUUID()};
  const message = {id:crypto.randomUUID(),sender:'codex:a',recipient:'b/xyz/codex:b',repo_id:'xyz',body:'retained',
    ...(kind === 'coordination' ? {coordination:{...metadata,operation:'assign'}} : {}),
    ...(kind === 'availability' ? {availability:{...metadata,kind:'joined'}} : {})};
  expect((await typedRequest(kind,'a','messages',message)).status).toBe(202);
  await age(message.id,2*DAY-1);
  await dispatch(bindings,message.id);
  expect(await (await typedRequest(kind,'b','inbox?repo_id=xyz')).json()).toMatchObject([{id:message.id}]);
  expect((await request('b','receipts',{id:message.id})).status).toBe(200);
  await bindings.DB.prepare('UPDATE messages SET received_at=NULL,ready=0 WHERE id=?').bind(message.id).run();
  for (const boundary of [2*DAY,2*DAY+1]) {
    await age(message.id,boundary);
    await dispatch(bindings,message.id);
    expect(await bindings.DB.prepare('SELECT ready FROM messages WHERE id=?').bind(message.id).first('ready')).toBe(0);
    await bindings.DB.prepare('UPDATE messages SET ready=1 WHERE id=?').bind(message.id).run();
    expect(await (await typedRequest(kind,'b','inbox?repo_id=xyz')).json()).toEqual([]);
    expect((await request('b','receipts',{id:message.id})).status).toBe(403);
    expect(await (await request('a',`status?id=${message.id}`)).json()).toMatchObject({status:'expired'});
    await bindings.DB.prepare('UPDATE messages SET ready=0 WHERE id=?').bind(message.id).run();
  }
  await age(message.id,14*DAY);
  await purge(bindings);
  expect(await bindings.DB.prepare('SELECT id FROM messages WHERE id=?').bind(message.id).first()).toBeNull();
  expect(await bindings.DB.prepare('SELECT id FROM purged_message_ids WHERE id=?').bind(message.id).first()).not.toBeNull();
});

it('keeps receipt retries, acknowledgement, and delivered status after 48 hours until retention ends', async () => {
  const time = clock();
  const {message} = await send();
  await dispatch(bindings,message.id);
  expect((await request('b','receipts',{id:message.id})).status).toBe(200);
  time.mockReturnValue(NOW+2*DAY);
  expect((await request('b','receipts',{id:message.id})).status).toBe(200);
  expect(await (await request('a',`status?id=${message.id}`)).json()).toMatchObject({status:'delivered_local'});
  expect((await request('b','ack',{id:message.id})).status).toBe(200);
  expect(await (await request('a',`status?id=${message.id}`)).json()).toMatchObject({status:'acknowledged'});
  time.mockReturnValue(NOW+14*DAY-1);
  expect((await request('b','ack',{id:message.id})).status).toBe(200);
  for (const boundary of [14*DAY,14*DAY+1]) {
    time.mockReturnValue(NOW+boundary);
    for (const endpoint of ['receipts','ack']) expect((await request('b',endpoint,{id:message.id})).status).toBe(403);
    expect((await request('a',`status?id=${message.id}`)).status).toBe(403);
    expect((await request('a','messages',message)).status).toBe(403);
  }
  await purge(bindings);
  expect(await bindings.DB.prepare('SELECT id FROM messages WHERE id=?').bind(message.id).first()).toBeNull();
});

it('repairs only live pending messages and purges through the existing scheduled hook', async () => {
  clock();
  const recent = await send(), expired = await send(), deleted = await send();
  await age(recent.message.id,2*DAY-1);
  await age(expired.message.id,2*DAY);
  await age(deleted.message.id,14*DAY);
  const queue = vi.mocked(bindings.DELIVERY.sendBatch);
  queue.mockClear();
  await worker.scheduled({} as ScheduledController,bindings);
  expect(queue).toHaveBeenCalledExactlyOnceWith([{body:recent.message.id}]);
  expect(await bindings.DB.prepare('SELECT id FROM messages WHERE id=?').bind(deleted.message.id).first()).toBeNull();
  await dispatch(bindings,deleted.message.id);
  expect((await request('a','messages',deleted.message)).status).toBe(403);
});

it('detaches recent replies, removes reports, and preserves accepted child identity across purge and concurrent retries', async () => {
  clock();
  const {message:parent} = await send();
  const child = {...parent,id:crypto.randomUUID(),body:'recent reply',reply_to:parent.id};
  expect((await request('a','messages',child)).status).toBe(202);
  await dispatch(bindings,parent.id);
  await request('b','receipts',{id:parent.id});
  expect((await request('b','reports',{message_id:parent.id,expected_revision:0,status:'received',reason:null})).status).toBe(200);
  await age(parent.id,14*DAY);
  expect((await request('a','messages',{...child,id:crypto.randomUUID()})).status).toBe(403);
  await bindings.DB.prepare("CREATE TRIGGER fail_purge BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'injected purge failure'); END").run();
  await expect(purge(bindings)).rejects.toThrow();
  expect(await bindings.DB.prepare('SELECT reply_to FROM messages WHERE id=?').bind(child.id).first('reply_to')).toBe(parent.id);
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS n FROM message_reports').first('n')).toBe(1);
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS n FROM purged_message_ids').first('n')).toBe(0);
  await bindings.DB.prepare('DROP TRIGGER fail_purge').run();
  await purge(bindings);
  expect(await bindings.DB.prepare('SELECT reply_to,accepted_reply_to,body,sent_at FROM messages WHERE id=?').bind(child.id).first())
    .toEqual({reply_to:null,accepted_reply_to:parent.id,body:'recent reply',sent_at:new Date(NOW).toISOString()});
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS n FROM message_reports').first('n')).toBe(0);
  expect(await bindings.DB.prepare('PRAGMA foreign_key_check').all()).toMatchObject({results:[]});
  await dispatch(bindings,child.id);
  expect(await (await request('b','inbox?repo_id=xyz')).json()).toMatchObject([{id:child.id,reply_to:parent.id}]);
  const retries = await Promise.all([request('a','messages',child),request('a','messages',parent),purge(bindings)]);
  expect((retries[0] as Response).status).toBe(200);
  expect((retries[1] as Response).status).toBe(403);
  expect((await request('a','messages',{...child,id:crypto.randomUUID()})).status).toBe(403);
  expect((await request('a',`reports?message_id=${parent.id}`)).status).toBe(403);
});

it('drains the oldest bounded batches and rolls back every purge effect on a dependency failure', async () => {
  clock();
  const timestamp = new Date(NOW-14*DAY).toISOString();
  await bindings.DB.batch(Array.from({length:BATCH+2},(_,index)=>bindings.DB.prepare(
    'INSERT INTO messages(id,sender,recipient,repo_id,body,sent_at,retention_at) VALUES (?,\'a/xyz/codex:a\',\'b/xyz/codex:b\',\'xyz\',\'old\',?,?)'
  ).bind(crypto.randomUUID(),timestamp,new Date(Date.parse(timestamp)+index-BATCH-1).toISOString())));
  const oldest = await bindings.DB.prepare('SELECT id FROM messages ORDER BY retention_at,sequence LIMIT 1').first<string>('id');
  await bindings.DB.prepare("CREATE TRIGGER fail_purge BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'injected purge failure'); END").run();
  await expect(purge(bindings)).rejects.toThrow();
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS n FROM purged_message_ids').first('n')).toBe(0);
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS n FROM messages').first('n')).toBe(BATCH+2);
  await bindings.DB.prepare('DROP TRIGGER fail_purge').run();
  await purge(bindings);
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS n FROM messages').first('n')).toBe(2);
  expect(await bindings.DB.prepare('SELECT id FROM purged_message_ids WHERE id=?').bind(oldest).first()).not.toBeNull();
  await purge(bindings); await purge(bindings);
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS n FROM messages').first('n')).toBe(0);
  expect(await bindings.DB.prepare('SELECT COUNT(*) AS n FROM purged_message_ids').first('n')).toBe(BATCH+2);
  const plan = await bindings.DB.prepare('EXPLAIN QUERY PLAN SELECT id FROM messages WHERE retention_at<=? ORDER BY retention_at,sequence LIMIT 100').bind(timestamp).all<{detail:string}>();
  expect(plan.results.some(row=>row.detail.includes('messages_retention'))).toBe(true);
});

it('intersects immutable legacy and stored report deadlines with retention before physical purge', async () => {
  clock();
  const {message} = await send();
  await dispatch(bindings,message.id); await request('b','receipts',{id:message.id});
  const report = {message_id:message.id,expected_revision:0,status:'received',reason:null};
  await age(message.id,13*DAY);
  await bindings.DB.prepare('UPDATE messages SET report_expires_at=NULL WHERE id=?').bind(message.id).run();
  expect((await request('b','reports',report)).status).toBe(200);
  expect(await (await request('a',`status?id=${message.id}`)).json()).toMatchObject({report_expires_at:new Date(NOW+17*DAY).toISOString()});
  await bindings.DB.prepare('UPDATE messages SET report_expires_at=? WHERE id=?').bind(new Date(NOW).toISOString(),message.id).run();
  expect((await request('a',`reports?message_id=${message.id}`)).status).toBe(403);
  expect((await request('b','reports',report)).status).toBe(403);
  await bindings.DB.prepare('UPDATE messages SET report_expires_at=NULL WHERE id=?').bind(message.id).run();
  await age(message.id,14*DAY);
  expect((await request('a',`reports?message_id=${message.id}`)).status).toBe(403);
  expect((await request('b','reports',report)).status).toBe(403);
});
