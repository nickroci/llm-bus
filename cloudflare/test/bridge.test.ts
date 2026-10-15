/** Real HTTP/Worker/D1/Queue/Python integration. All machines and wakes are isolated. */
import { Miniflare } from 'miniflare';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { expect, it } from 'vitest';
import { tokenHash } from '../src/index';

it('delivers across two Python mailboxes through HTTP and Queue, then synchronizes recipient ack', async () => {
  const mf = new Miniflare({modules:true, scriptPath:'dist/index.js', compatibilityDate:'2026-08-01',
    d1Databases:['DB'], queueProducers:{DELIVERY:'delivery'}, queueConsumers:{delivery:{maxBatchSize:1,maxBatchTimeout:0}}});
  const folder = mkdtempSync(join(tmpdir(), 'llm-bus-e2e-'));
  try {
    const db = await mf.getD1Database('DB');
    const migration = readFileSync(new URL('../migrations/0001_bus.sql', import.meta.url), 'utf8');
    for (const statement of migration.split(';').filter(s=>s.trim())) await db.prepare(statement).run();
    for (const machine of ['a','b']) {
      await db.prepare('INSERT INTO machines(id,token_hash) VALUES (?,?)').bind(machine,await tokenHash(machine.repeat(43))).run();
      await db.prepare('INSERT INTO repo_grants VALUES (?,?)').bind(machine,'xyz').run();
    }
    const origin = (await mf.ready).origin;
    const program = `
import sys,time
from pathlib import Path
from llm_bus_remote import Bridge,Config,Repository,enqueue_remote
from llm_bus_remote_store import RemoteStore
from llm_bus_store import Store,SenderContext,routing_scope
import llm_bus_remote
root=Path(sys.argv[1]); origin=sys.argv[2]
# No actual agent host is addressed by this isolated integration.
llm_bus_remote.wake=lambda *args,**kwargs:{"status":"requested","via":"test"}
repo=root/'repo'; repo.mkdir()
configs={}; stores={}
for machine in ('a','b'):
    token=root/(machine+'.token'); token.write_text(machine*43); token.chmod(0o600)
    configs[machine]=Config(origin,machine,token,(Repository(routing_scope(str(repo)),'xyz',send=True,receive=True),))
    stores[machine]=Store(root/(machine+'.sqlite3'))
stores['b'].register('claude','recipient',str(repo))
for machine in ('a','b'): assert Bridge(stores[machine],configs[machine]).sync()['errors']==0
sent=enqueue_remote(stores['a'],configs['a'],SenderContext('codex','sender',str(repo)),'b/xyz/claude:recipient','remote hello')
# Move the sender before any bridge has exported its original registration.
stores['a'].register('codex','sender',str(root/'elsewhere'))
assert Bridge(stores['a'],configs['a']).sync()['errors']==0
for attempt in range(50):
    # Reopen SQLite and rebuild bridge every time to prove restart safety.
    destination=Store(stores['b'].path)
    assert Bridge(destination,configs['b']).sync()['errors']==0
    inbox=destination.inbox('claude:recipient')
    if inbox: break
    time.sleep(.05)
assert len(inbox)==1 and inbox[0]['body']=='remote hello'
assert inbox[0]['sender']=='a/xyz/codex:sender'
assert Bridge(destination,configs['b']).sync()['errors']==0
assert len(destination.inbox('claude:recipient'))==1
destination.acknowledge('claude:recipient',[inbox[0]['id']])
assert Bridge(destination,configs['b']).sync()['errors']==0
assert Bridge(Store(stores['a'].path),configs['a']).sync()['errors']==0
status=RemoteStore(stores['a'],configs['a'].connection).statuses()[0]
assert status['status']=='acknowledged',status
assert status['remote_id']==sent['remote_id']
print('cross-machine delivery and acknowledgement passed')
`;
    const result = await promisify(execFile)('uv', ['run','python','-c',program,folder,origin], {cwd: new URL('../..',import.meta.url).pathname, timeout:30000});
    expect(result.stdout).toContain('cross-machine delivery and acknowledgement passed');
  } finally { await mf.dispose(); rmSync(folder,{recursive:true,force:true}); }
},40000);
