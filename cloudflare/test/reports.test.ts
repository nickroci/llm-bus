import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { dispatch } from '../src/messages';
import { bindings, request, send } from './relay-fixture';
const reportCases = JSON.parse(readFileSync(new URL('../../tests/fixtures/report-cases.json', import.meta.url), 'utf8')) as {status: unknown; reason: unknown; valid: boolean}[];
describe('typed reports', () => {
  it.each(reportCases)('validates vocabulary $status $reason', async ({status, reason, valid}) => {
    const {message} = await send(); await dispatch(bindings, message.id);
    await request('b', 'receipts', {id: message.id});
    expect((await request('b', 'reports', {message_id: message.id, expected_revision: 0, status, reason})).status).toBe(valid ? 200 : 400);
  });
  it('binds ownership, receipt, exact fields and revision types', async () => {
    const {message} = await send();
    const report = {message_id: message.id, expected_revision: 0, status: 'received', reason: null};
    expect((await request('b', 'reports', report)).status).toBe(403);
    await dispatch(bindings, message.id); await request('b', 'receipts', {id: message.id});
    expect((await request('a', 'reports', report)).status).toBe(403);
    expect((await request('b', 'reports', {...report, message_id: crypto.randomUUID()})).status).toBe(403);
    expect((await request('b', 'reports', {...report, body: 'text'})).status).toBe(400);
    expect((await request('b', 'reports', {...report, expected_revision: true})).status).toBe(400);
    expect((await request('b', `reports?message_id=${message.id}`)).status).toBe(403);
    expect(await (await request('a', `reports?message_id=${message.id}`)).json()).toEqual({message_id:message.id,revision:0,status:null,reason:null});
  });
  it('atomically rejects competing revisions and preserves terminal states', async () => {
    const {message} = await send(); await dispatch(bindings, message.id); await request('b','receipts',{id:message.id});
    const first = {message_id:message.id,expected_revision:0,status:'proceeding',reason:'started'};
    const results = await Promise.all([request('b','reports',first), request('b','reports',{...first,status:'not_proceeding',reason:'declined'})]);
    expect(results.map(r=>r.status).sort()).toEqual([200,409]);
    const latest = await (await request('a',`reports?message_id=${message.id}`)).json() as {status:string;reason:string};
    expect((await request('b','reports',{...first,status:latest.status,reason:latest.reason})).status).toBe(200);
    expect((await request('b','reports',{...first,expected_revision:1,status:'received',reason:null})).status).toBe(409);
    const terminal={...first,expected_revision:1,status:'completed',reason:null};
    expect((await request('b','reports',terminal)).status).toBe(200);
    expect((await request('b','reports',terminal)).status).toBe(200);
    expect((await request('b','reports',{...terminal,expected_revision:2,status:'failed',reason:'execution_failed'})).status).toBe(409);
    await request('b','ack',{id:message.id});
    expect(await (await request('a',`reports?message_id=${message.id}`)).json()).toEqual({message_id:message.id,revision:2,status:'completed',reason:null});
    await bindings.DB.prepare("DELETE FROM repo_grants WHERE machine_id='b'").run();
    expect((await request('a',`reports?message_id=${message.id}`)).status).toBe(403);
  });
});

it('returns its own CAS result when a newer revision commits before response delivery', async () => {
  const {message} = await send(); await dispatch(bindings,message.id); await request('b','receipts',{id:message.id});
  const original = bindings.DB;
  let advance = true;
  bindings.DB = new Proxy(original, {get(target,key) {
    if (key !== 'prepare') return Reflect.get(target,key,target);
    return (sql:string) => {
      const statement = target.prepare(sql);
      if (!sql.includes('RETURNING message_id')) return statement;
      return new Proxy(statement,{get(inner,key) {
        if (key !== 'bind') return Reflect.get(inner,key,inner);
        return (...args:unknown[]) => {
          const bound=inner.bind(...args);
          return new Proxy(bound,{get(query,key) {
            if (key !== 'first') return Reflect.get(query,key,query);
            return async () => {
              const row=await query.first<{revision:number}>();
              if (advance && row?.revision===1) {
                advance=false;
                expect((await request('b','reports',{message_id:message.id,expected_revision:1,status:'completed',reason:null})).status).toBe(200);
              }
              return row;
            };
          }});
        };
      }});
    };
  }});
  try {
    const response=await request('b','reports',{message_id:message.id,expected_revision:0,status:'proceeding',reason:'started'});
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({message_id:message.id,revision:1,status:'proceeding',reason:'started'});
    expect(await (await request('a',`reports?message_id=${message.id}`)).json()).toEqual({message_id:message.id,revision:2,status:'completed',reason:null});
  } finally {bindings.DB=original;}
});
it('keeps relay-owned report deadlines stable on retries and refuses expired reports', async () => {
  const {message,response}=await send();
  const accepted=await response.json() as {report_expires_at:string};
  expect(accepted.report_expires_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  expect(await (await request('a','messages',message)).json()).toMatchObject({report_expires_at:accepted.report_expires_at});
  await dispatch(bindings,message.id);await request('b','receipts',{id:message.id});
  await bindings.DB.prepare("UPDATE messages SET retention_at='2020-01-01T00:00:00.000Z' WHERE id=?").bind(message.id).run();
  expect((await request('a',`reports?message_id=${message.id}`)).status).toBe(403);
  expect((await request('b','reports',{message_id:message.id,expected_revision:0,status:'received',reason:null})).status).toBe(403);
});
