import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import worker from '../src/index';
import { dispatch, repair } from '../src/messages';
import { bindings, request, register, send } from './relay-fixture';
describe('machine and repository boundary', () => {
  it('adds availability metadata to the prior schema without changing history', async () => {
    await bindings.DB.prepare('ALTER TABLE messages DROP COLUMN availability').run();
    const id=crypto.randomUUID();
    await bindings.DB.prepare("INSERT INTO messages(id,sender,recipient,repo_id,body,retention_at) VALUES (?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
      .bind(id,'a/xyz/codex:a','b/xyz/codex:b','xyz','retained').run();
    const migration=readFileSync(new URL('../migrations/0006_coordination_availability.sql',import.meta.url),'utf8');
    await bindings.DB.prepare(migration).run();
    expect(await bindings.DB.prepare('SELECT body,availability FROM messages WHERE id=?').bind(id).first())
      .toEqual({body:'retained',availability:null});
    expect((await request('a','messages',{id,sender:'codex:a',recipient:'b/xyz/codex:b',repo_id:'xyz',body:'retained'})).status).toBe(200);
  });
  it('isolates strict availability events from ordinary messages and directions', async () => {
    const call = (machine:string, path:string, data?:object) => worker.fetch(new Request(`https://bus.test/v2/coordination/${path}`, {
      method:data ? 'POST' : 'GET', headers:{Authorization:`Bearer ${machine.repeat(43)}`,'Content-Type':'application/json'}, body:data ? JSON.stringify(data) : undefined,
    }),bindings);
    expect(await (await call('a','availability/capabilities')).json()).toEqual({availability_version:1});
    const metadata={version:1,project_id:crypto.randomUUID(),generation:crypto.randomUUID(),grant_id:crypto.randomUUID(),kind:'joined'};
    const message={id:crypto.randomUUID(),sender:'codex:a',recipient:'b/xyz/codex:b',repo_id:'xyz',body:'joined',availability:metadata};
    expect((await request('a','messages',message)).status).toBe(400);
    expect((await call('a','messages',message)).status).toBe(400);
    expect((await call('a','availability/messages',message)).status).toBe(202);
    expect((await call('a','availability/messages',message)).status).toBe(200);
    for (const key of ['grant_id','generation','project_id']) {
      expect((await call('a','availability/messages',{...message,availability:{...metadata,[key]:crypto.randomUUID()}})).status).toBe(409);
    }
    for (const bad of [{...metadata,version:true},{...metadata,extra:true},{...metadata,kind:'assign'},{...metadata,grant_id:'bad'},null]) {
      expect((await call('a','availability/messages',{...message,id:crypto.randomUUID(),availability:bad})).status).toBe(400);
    }
    expect((await call('a','availability/messages',{...message,coordination:{}})).status).toBe(400);
    expect((await call('c','availability/messages',{...message,id:crypto.randomUUID()})).status).toBe(403);
    await dispatch(bindings,message.id);
    expect(await (await request('b','inbox?repo_id=xyz')).json()).toEqual([]);
    expect(await (await call('b','inbox?repo_id=xyz')).json()).toEqual([]);
    const page=await (await call('b','availability/inbox?repo_id=xyz')).json() as Record<string,unknown>[];
    expect(page).toMatchObject([{id:message.id,availability:metadata,sender:'a/xyz/codex:a'}]);
    expect(page[0]).not.toHaveProperty('coordination');
  });
  it('orders labels and releases independently from activity and legacy clients', async () => {
    const base = {repo_id:'xyz',native_address:'codex:a',last_seen:new Date().toISOString(),ended:false};
    expect((await request('a','sessions',{...base,label:'Worker',revision:2})).status).toBe(200);
    expect((await request('a','sessions',{...base,label:'Stale',revision:1})).status).toBe(200);
    expect((await request('a','sessions',{...base,label:'Conflict',revision:2})).status).toBe(200);
    expect((await request('a','sessions',{...base,ended:true})).status).toBe(200);
    let row=await bindings.DB.prepare('SELECT * FROM sessions WHERE address=?').bind('a/xyz/codex:a').first();
    expect(row).toMatchObject({label:'Worker',revision:2,ended:0});
    expect((await request('a','sessions',{...base,label:'Worker',revision:3,ended:true})).status).toBe(200);
    row=await bindings.DB.prepare('SELECT * FROM sessions WHERE address=?').bind('a/xyz/codex:a').first();
    expect(row).toMatchObject({label:null,revision:3,ended:1});
    for (const metadata of [{label:'bad/name',revision:4},{label:'Worker',revision:0},{revision:true},{revision:-1}]) {
      expect((await request('a','sessions',{...base,...metadata})).status).toBe(400);
    }
    await bindings.DB.prepare("DELETE FROM repo_grants WHERE machine_id='a'").run();
    expect((await request('a','sessions',{...base,revision:1})).status).toBe(403);
  });
  it('isolates versioned directions and compares every immutable authority field', async () => {
    const call = (path:string, data?:object) => worker.fetch(new Request(`https://bus.test/v2/coordination/${path}`, {
      method:data ? 'POST' : 'GET', headers:{Authorization:`Bearer ${'a'.repeat(43)}`,'Content-Type':'application/json'}, body:data ? JSON.stringify(data) : undefined,
    }),bindings);
    expect(await (await call('capabilities')).json()).toEqual({version:1});
    const metadata={version:1,project_id:crypto.randomUUID(),generation:crypto.randomUUID(),grant_id:crypto.randomUUID(),operation:'assign'};
    const message={id:crypto.randomUUID(),sender:'codex:a',recipient:'b/xyz/codex:b',repo_id:'xyz',body:'direction',coordination:metadata};
    expect((await request('a','messages',message)).status).toBe(400);
    expect((await call('messages',message)).status).toBe(202);
    expect((await call('messages',{...message,coordination:{...metadata}})).status).toBe(200);
    for (const key of ['grant_id','generation','project_id']) {
      expect((await call('messages',{...message,coordination:{...metadata,[key]:crypto.randomUUID()}})).status).toBe(409);
    }
    for (const bad of [{...metadata,version:2},{...metadata,extra:true},{...metadata,operation:['assign']},{...metadata,grant_id:'bad'}]) {
      expect((await call('messages',{...message,id:crypto.randomUUID(),coordination:bad})).status).toBe(400);
    }
    await dispatch(bindings,message.id);
    expect(await (await request('b','inbox?repo_id=xyz')).json()).toEqual([]);
    const result=await worker.fetch(new Request('https://bus.test/v2/coordination/inbox?repo_id=xyz',{headers:{Authorization:`Bearer ${'b'.repeat(43)}`}}),bindings);
    expect(await result.json()).toMatchObject([{id:message.id,coordination:metadata,sender:'a/xyz/codex:a'}]);
  });
  it('compares the reply UUID again after a raced duplicate insert', async () => {
    const {message} = await send();
    const reply = {id:crypto.randomUUID(),sender:'codex:b',recipient:'a/xyz/codex:a',repo_id:'xyz',body:'reply',reply_to:message.id};
    expect((await request('b','messages',reply)).status).toBe(202);
    const original=bindings.DB;
    for (const variant of ['same','reply','availability']) {
      let hidePrior=true;
      bindings.DB=new Proxy(original,{get(target,key) {
        if (key!=='prepare') return Reflect.get(target,key,target);
        return (sql:string) => {
          const statement=target.prepare(sql);
          if (sql!=='SELECT * FROM messages WHERE id=?') return statement;
          return new Proxy(statement,{get(inner,key) {
            if (key!=='bind') return Reflect.get(inner,key,inner);
            return (...args:unknown[]) => {
              const bound=inner.bind(...args);
              return new Proxy(bound,{get(query,key) {
                if (key!=='first') return Reflect.get(query,key,query);
                return async () => {
                  if (hidePrior) {hidePrior=false; return null;}
                  return query.first();
                };
              }});
            };
          }});
        };
      }});
      try {
        if (variant === 'availability') await original.prepare('UPDATE messages SET availability=? WHERE id=?').bind('{}',reply.id).run();
        const result=await request('b','messages',{...reply,reply_to:variant === 'reply' ? undefined : message.id});
        expect(result.status).toBe(variant === 'same' ? 200 : 409);
      } finally {bindings.DB=original;}
    }
  });
  it('advertises reply support and binds links to participants, repository, and immutable UUID', async () => {
    expect(await (await request('a','capabilities')).json()).toEqual({reply_to:true,batch_status:true,batch_reports:true});
    const {message} = await send();
    const reply = {id:crypto.randomUUID(),sender:'codex:b',recipient:'a/xyz/codex:a',repo_id:'xyz',body:'reply',reply_to:message.id};
    expect((await request('b','messages',reply)).status).toBe(202);
    expect((await request('b','messages',reply)).status).toBe(200);
    expect((await request('b','messages',{...reply,reply_to:undefined})).status).toBe(409);
    await register('b','xyz','codex:other');
    expect((await request('b','messages',{...reply,id:crypto.randomUUID(),sender:'codex:other'})).status).toBe(403);
    expect((await request('b','messages',{...reply,id:crypto.randomUUID(),reply_to:crypto.randomUUID()})).status).toBe(403);
    expect((await request('b','messages',{...reply,id:crypto.randomUUID(),reply_to:1})).status).toBe(400);
    expect((await request('b','messages',{...reply,id:crypto.randomUUID(),reply_to:null})).status).toBe(400);
    await dispatch(bindings,reply.id);
    expect(await (await request('a','inbox?repo_id=xyz')).json()).toMatchObject([{reply_to:message.id}]);
    await bindings.DB.prepare("DELETE FROM repo_grants WHERE machine_id='a'").run();
    expect((await request('b','messages',{...reply,id:crypto.randomUUID()})).status).toBe(403);
  });
  it('batch reads preserve item authorization and reject malformed requests', async () => {
    const {message} = await send();
    const denied = crypto.randomUUID();
    expect((await request('b', 'messages', {id:denied, sender:'codex:b', recipient:'a/xyz/codex:a', repo_id:'xyz', body:'private neighbor'})).status).toBe(202);
    for (const [route, field] of [['status/batch', 'ids'], ['reports/batch', 'message_ids']]) {
      const result = await request('a', route, {[field]: [message.id, denied]});
      expect(result.status).toBe(200);
      expect(await result.json()).toMatchObject({results: [
        {id: message.id, error: null, value: route.startsWith('status') ? {id:message.id} : {message_id:message.id}},
        {id: denied, value: null, error: {code:'not_allowed'}},
      ]});
      for (const data of [{[field]:[]}, {[field]:[message.id,message.id]}, {[field]:['invalid']}, {[field]:[message.id], extra:1}, {[field]:Array.from({length:101},()=>crypto.randomUUID())}]) {
        expect((await request('a',route,data)).status).toBe(400);
      }
    }
    await bindings.DB.prepare("DELETE FROM repo_grants WHERE machine_id='a'").run();
    for (const [route, field] of [['status/batch', 'ids'], ['reports/batch', 'message_ids']]) {
      expect(await (await request('a',route,{[field]:[message.id]})).json()).toEqual({results:[{id:message.id,value:null,error:{code:'not_allowed'}}]});
    }
  });
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
    expect(await (await request('a', `status?id=${message.id}`)).json()).toMatchObject({id: message.id, status: 'acknowledged', report_expires_at: expect.any(String)});
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
    const {message,response} = await send();
    const accepted = await response.json() as {report_expires_at:string;retention_expires_at:string};
    await repair(bindings);
    expect(await bindings.DB.prepare('SELECT id FROM messages WHERE ready=0').first()).not.toBeNull();
    const age = Date.now()-2*86400_000;
    await bindings.DB.prepare('UPDATE messages SET retention_at=? WHERE id=?').bind(new Date(age).toISOString(),message.id).run();
    expect(await (await request('a', `status?id=${message.id}`)).json()).toEqual({...accepted,status:'expired',retention_expires_at:new Date(age+14*86400_000).toISOString()});
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
