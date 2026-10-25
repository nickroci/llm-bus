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
    const migration = ['0001_bus.sql','0002_reports.sql','0003_reply_to.sql'].map(name=>readFileSync(new URL(`../migrations/${name}`, import.meta.url), 'utf8')).join(';');
    for (const statement of migration.split(';').filter(s=>s.trim())) await db.prepare(statement).run();
    for (const machine of ['a','b']) {
      await db.prepare('INSERT INTO machines(id,token_hash) VALUES (?,?)').bind(machine,await tokenHash(machine.repeat(43))).run();
      await db.prepare('INSERT INTO repo_grants VALUES (?,?)').bind(machine,'xyz').run();
    }
    const origin = (await mf.ready).origin;
    const program = `
import os,sys,time
from pathlib import Path
from llm_bus_remote import Bridge,enqueue_remote
from llm_bus_policy import Config,Repository,PeerPolicy
from llm_bus_remote_store import RemoteStore
from llm_bus_reports import Reports
from llm_bus_store import Store,SenderContext,routing_scope
import llm_bus_dispatch
import llm_bus_wake
def forbid_native(*args,**kwargs):
    raise AssertionError('isolated integration must not execute native adapters')
llm_bus_wake._wake_codex=forbid_native
llm_bus_wake._wake_claude=forbid_native
root=Path(sys.argv[1]); origin=sys.argv[2]
from datetime import UTC,datetime,timedelta
clock_now=[datetime.now(UTC)]
def bridge(store,config):
    # Each explicit synchronization observes a new due-work boundary.
    clock_now[0]+=timedelta(seconds=60)
    return Bridge(store,config,clock=lambda:clock_now[0])
# No actual agent host is addressed by this isolated integration.
wakes=[]
def wake(*args,**kwargs):
    wakes.append(args)
    return {"status":"requested","via":"test"}
llm_bus_dispatch.wake=wake
repo=root/'repo'; repo.mkdir()
configs={}; stores={}
def select(machine):
    os.environ["LLM_BUS_CONFIG"]=str(root/(machine+".toml"))
    return stores[machine]
for machine in ('a','b'):
    token=root/(machine+'.token'); token.write_text(machine[0]*43); token.chmod(0o600)
    configs[machine]=Config(origin,machine,token,(Repository(routing_scope(str(repo)),'xyz',(PeerPolicy('b' if machine=='a' else 'a',True,'inbox'),)),))
    stores[machine]=Store(root/(machine+'.sqlite3'))
    other='b' if machine=='a' else 'a'
    (root/(machine+'.toml')).write_text(f"""policy_version = 2
[remote]
url = "{origin}"
machine_id = "{machine}"
credential_file = "{token}"
[[repositories]]
path = "{repo}"
repo_id = "xyz"
[[repositories.peers]]
machine_id = "{other}"
send_messages = true
receive_messages = "inbox"
send_reports = ["received", "completed"]
receive_reports = ["received", "completed"]
""")
stores['b'].register('claude','recipient',str(repo))
for machine in ('a','b'): assert bridge(select(machine),configs[machine]).sync()['errors']==0
sent=enqueue_remote(stores['a'],configs['a'],SenderContext('codex','sender',str(repo)),'b/xyz/claude:recipient','remote hello')
# Move the sender before any bridge has exported its original registration.
stores['a'].register('codex','sender',str(root/'elsewhere'))
assert bridge(select('a'),configs['a']).sync()['errors']==0
for attempt in range(50):
    # Reopen SQLite and rebuild bridge every time to prove restart safety.
    select('b')
    destination=Store(stores['b'].path)
    assert bridge(destination,configs['b']).sync()['errors']==0
    inbox=destination.inbox('claude:recipient')
    if inbox: break
    time.sleep(.05)
assert len(inbox)==1 and inbox[0]['body']=='remote hello'
assert inbox[0]['sender']=='a/xyz/codex:sender'
assert bridge(destination,configs['b']).sync()['errors']==0
assert len(destination.inbox('claude:recipient'))==1
destination.acknowledge('claude:recipient',[inbox[0]['id']])
assert bridge(destination,configs['b']).sync()['errors']==0
assert bridge(Store(select('a').path),configs['a']).sync()['errors']==0
status=RemoteStore(stores['a'],configs['a'].connection).statuses()[0]
assert status['status']=='acknowledged',status
assert status['remote_id']==sent['remote_id']
select('b')
reports=Reports(destination,configs['b'].connection)
reports.enqueue(inbox[0]['id'],'claude:recipient','received',None)
assert bridge(destination,configs['b']).sync()['errors']==0
assert bridge(select('a'),configs['a']).sync()['errors']==0
assert Reports(stores['a'],configs['a'].connection).statuses()[0]['work_report']['status']=='received'
select('b')
reports.enqueue(inbox[0]['id'],'claude:recipient','completed',None)
assert bridge(destination,configs['b']).sync()['errors']==0
assert bridge(select('a'),configs['a']).sync()['errors']==0
assert Reports(stores['a'],configs['a'].connection).statuses()[0]['work_report']['status']=='completed'
assert len(wakes)==1
assert len(stores['a'].history(folder=None))==1
assert len(stores['b'].history(folder=None))==1
# Move the participant back to its captured repository before replying.
stores['a'].register('codex','sender',str(repo))
select('b')
stores['b'].register('codex','noise',str(repo))
noise=stores['b'].send(SenderContext('claude','recipient',str(repo)),'codex:noise','local only')
stores['b'].acknowledge('codex:noise',[noise['id']])
reply=enqueue_remote(stores['b'],configs['b'],SenderContext('claude','recipient',str(repo)),
    'a/xyz/codex:sender','answer',reply_to=inbox[0]['id'])
assert bridge(select('b'),configs['b']).sync()['errors']==0
for attempt in range(50):
    assert bridge(select('a'),configs['a']).sync()['errors']==0
    answers=stores['a'].inbox('codex:sender')
    if answers: break
    time.sleep(.05)
assert answers[0]['reply_to']==sent['id']
assert answers[0]['id']!=reply['id']
followup=enqueue_remote(stores['a'],configs['a'],SenderContext('codex','sender',str(repo)),
    'b/xyz/claude:recipient','follow up',reply_to=answers[0]['id'])
assert bridge(select('a'),configs['a']).sync()['errors']==0
for attempt in range(50):
    assert bridge(select('b'),configs['b']).sync()['errors']==0
    followups=stores['b'].inbox('claude:recipient')
    if followups: break
    time.sleep(.05)
assert followups[0]['reply_to']==reply['id']
assert len(stores['a'].history(folder=None,conversation=sent['id']))==3
assert len(stores['b'].history(folder=None,conversation=inbox[0]['id']))==3
print('cross-machine delivery and acknowledgement passed')
`;
    const result = await promisify(execFile)('uv', ['run','python','-c',program,folder,origin], {cwd: new URL('../..',import.meta.url).pathname, timeout:30000});
    expect(result.stdout).toContain('cross-machine delivery and acknowledgement passed');
  } finally { await mf.dispose(); rmSync(folder,{recursive:true,force:true}); }
},40000);

it('enforces independent PC/VPS/outsider policies and keeps monitor replay outside native delivery', async () => {
  const mf = new Miniflare({modules:true, scriptPath:'dist/index.js', compatibilityDate:'2026-08-01',
    d1Databases:['DB'], queueProducers:{DELIVERY:'delivery'}, queueConsumers:{delivery:{maxBatchSize:1,maxBatchTimeout:0}}});
  const folder = mkdtempSync(join(tmpdir(), 'llm-bus-policy-e2e-'));
  try {
    const db = await mf.getD1Database('DB');
    const migration = ['0001_bus.sql','0002_reports.sql','0003_reply_to.sql'].map(name=>readFileSync(new URL(`../migrations/${name}`, import.meta.url), 'utf8')).join(';');
    for (const statement of migration.split(';').filter(s=>s.trim())) await db.prepare(statement).run();
    for (const machine of ['pc','vps','outsider']) {
      await db.prepare('INSERT INTO machines(id,token_hash) VALUES (?,?)').bind(machine,await tokenHash(machine[0].repeat(43))).run();
      await db.prepare('INSERT INTO repo_grants VALUES (?,?)').bind(machine,'xyz').run();
    }
    const origin = (await mf.ready).origin;
    const program = String.raw`
import json,os,sys,time
from pathlib import Path
from uuid import uuid4
from llm_bus_policy import load_config
from llm_bus_remote import Bridge,Relay,enqueue_remote
from llm_bus_remote_store import RemoteStore
from llm_bus_reports import Reports
from llm_bus_store import BusError,SenderContext,Store
import llm_bus_dispatch
import llm_bus_wake
def forbid_native(*args,**kwargs):
    raise AssertionError('isolated integration must not execute native adapters')
llm_bus_wake._wake_codex=forbid_native
llm_bus_wake._wake_claude=forbid_native
root=Path(sys.argv[1]); origin=sys.argv[2]
from datetime import UTC,datetime,timedelta
clock_now=[datetime.now(UTC)]
def bridge(store,config):
    # Each explicit synchronization observes a new due-work boundary.
    clock_now[0]+=timedelta(seconds=60)
    return Bridge(store,config,clock=lambda:clock_now[0])
repo=root/'repo'; repo.mkdir()
wakes=[]
def wake(native,*args,**kwargs):
    wakes.append(native)
    return {'status':'requested','via':'test'}
llm_bus_dispatch.wake=wake
stores={machine:Store(root/(machine+'.sqlite3')) for machine in ('pc','vps','outsider')}
policies={
 'pc':[('vps',True,'deny',[],['received','completed']),('outsider',True,'deny',[],[])],
 'vps':[('pc',False,'inbox',['received','completed'],[]),('outsider',False,'inbox',[],[])],
 'outsider':[('pc',True,'monitor',[],[]),('vps',True,'deny',[],[])]}
def write_policy(machine):
    token=root/(machine+'.token')
    text=f'''policy_version = 2
[remote]
url = "{origin}"
machine_id = "{machine}"
credential_file = "{token}"
[[repositories]]
path = "{repo}"
repo_id = "xyz"
'''
    for peer,send,receive,send_reports,receive_reports in policies[machine]:
        text+=f'''[[repositories.peers]]
machine_id = "{peer}"
send_messages = {str(send).lower()}
receive_messages = "{receive}"
send_reports = {json.dumps(send_reports)}
receive_reports = {json.dumps(receive_reports)}
'''
    (root/(machine+'.toml')).write_text(text)
def select(machine):
    os.environ['LLM_BUS_CONFIG']=str(root/(machine+'.toml'))
    config=load_config()
    assert config is not None
    return Store(stores[machine].path),config
def sync(machine):
    store,config=select(machine)
    result=bridge(store,config).sync()
    assert result['errors']==0,(machine,result)
    return result
def send(machine,destination,body):
    store,config=select(machine)
    return enqueue_remote(store,config,SenderContext('codex',machine,str(repo)),destination,body)
def wait_for(predicate):
    for attempt in range(100):
        if predicate(): return
        time.sleep(.03)
    raise AssertionError('local Queue delivery did not arrive')
def inbox(machine):
    store,_=select(machine)
    return store.inbox('codex:'+machine)
def relay_pending(machine):
    _,config=select(machine)
    return Relay(config).request('GET','/v1/inbox?repo_id=xyz&after=0')
for machine in stores:
    token=root/(machine+'.token'); token.write_text(machine[0]*43); token.chmod(0o600)
    write_policy(machine)
    # Existing supported native identities, not a monitoring product actor.
    stores[machine].register('codex',machine,str(repo))
    sync(machine)
# Offline enqueue and restart: PC's task reaches VPS through real HTTP/Queue.
task=send('pc','vps/xyz/codex:vps','perform work')
store,config=select('pc')
assert RemoteStore(store,config.connection).statuses()[0]['status']=='queued_local'
sync('pc')
def vps_received():
    sync('vps')
    return len(inbox('vps'))==1
wait_for(vps_received)
local_task=inbox('vps')[0]
# VPS accepts outsider text independently of PC's policy.
send('outsider','vps/xyz/codex:vps','outsider instruction'); sync('outsider')
wait_for(lambda: (sync('vps'),len(inbox('vps'))==2)[1])
outsider_task=next(row for row in inbox('vps') if row['sender'].startswith('outsider/'))
# Sender permission and relay admission never grant reverse local admission.
direct=send('outsider','pc/xyz/codex:pc','direct outsider instruction'); sync('outsider')
store,config=select('vps')
try:
    enqueue_remote(store,config,SenderContext('codex','vps',str(repo)),'pc/xyz/codex:pc',outsider_task['body'])
except BusError: pass
else: raise AssertionError('VPS normal sends must be locally disabled')
# A token holder can bypass its own client; destination policy must still hold.
forwarded={'id':str(uuid4()),'sender':'codex:vps','recipient':'pc/xyz/codex:pc',
 'repo_id':'xyz','body':outsider_task['body']}
assert Relay(config).request('POST','/v1/messages',forwarded)['status']=='accepted_remote'
wait_for(lambda: len(relay_pending('pc'))==2)
assert sync('pc')['imported']==0
pc,pc_config=select('pc')
assert pc.inbox('codex:pc')==[] and pc.pending_count('codex:pc')==0
assert RemoteStore.monitor_history(pc,repository=None)==[]
assert wakes==['codex:vps','codex:vps'],wakes
# Blocked text is still transport-accepted at the relay, never locally delivered.
_,outsider_config=select('outsider')
assert Relay(outsider_config).request('GET','/v1/status?id='+direct['remote_id'])['status']=='accepted_remote'
# Acknowledgement is independent: reports synchronize after ack.
vps,vps_config=select('vps')
vps.acknowledge('codex:vps',[local_task['id']]); sync('vps'); sync('pc')
pc,pc_config=select('pc')
assert Reports(pc,pc_config.connection).statuses()[0]['status']=='acknowledged'
vps,vps_config=select('vps')
reports=Reports(vps,vps_config.connection)
for status,reason in [('custom status',None),('completed','arbitrary text')]:
    try: reports.enqueue(local_task['id'],'codex:vps',status,reason)
    except BusError: pass
    else: raise AssertionError('free text report accepted locally')
for payload in [
 {'message_id':task['remote_id'],'expected_revision':0,'status':'completed','reason':'arbitrary text'},
 {'message_id':task['remote_id'],'expected_revision':0,'status':'completed','reason':None,'body':'injected text'}]:
    try: Relay(vps_config).request('POST','/v1/reports',payload)
    except BusError as error: assert 'HTTP 400' in str(error)
    else: raise AssertionError('free text report accepted by relay')
reports.enqueue(local_task['id'],'codex:vps','received',None)
sync('vps'); sync('pc')
pc,pc_config=select('pc')
assert Reports(pc,pc_config.connection).statuses()[0]['work_report']['status']=='received'
vps,vps_config=select('vps')
# A false valid claim remains data; reporting executes no work.
Reports(vps,vps_config.connection).enqueue(local_task['id'],'codex:vps','completed',None)
sync('vps'); sync('pc')
pc,pc_config=select('pc')
result=Reports(pc,pc_config.connection).statuses()[0]
assert result['status']=='acknowledged'
assert result['work_report']=={'message_id':task['remote_id'],'revision':2,'status':'completed','reason':None}
assert pc.inbox('codex:pc')==[] and pc.pending_count('codex:pc')==0
assert len(pc.history(folder=None))==1
assert wakes==['codex:vps','codex:vps']
# Third machine has monitor-only reception for PC, addressed to its native session.
monitored=send('pc','outsider/xyz/codex:outsider','human-only \x1b[2J text'); sync('pc')
wait_for(lambda: len(relay_pending('outsider'))==1)
request=Relay.request
lost=[]
def lose_receipt(self,method,path,data=None):
    if self.config.machine_id=='outsider' and path=='/v1/receipts' and not lost:
        # Network loss before HTTP receipt leaves real relay mail available for replay.
        assert len(RemoteStore.monitor_history(Store(stores['outsider'].path),repository=None))==1
        lost.append(data)
        raise BusError('isolated receipt connection lost')
    return request(self,method,path,data)
Relay.request=lose_receipt
store,config=select('outsider')
try:
    interrupted=bridge(store,config).sync()
    assert interrupted['imported']==0 and interrupted['errors']==1,interrupted
finally: Relay.request=request
# Change class before replay: first durable class wins across reconstruction.
policies['outsider'][0]=('pc',True,'inbox',[],[]); write_policy('outsider')
assert sync('outsider')['imported']==0
store,config=select('outsider')
rows=RemoteStore.monitor_history(store,repository=None)
assert len(rows)==1 and rows[0][3]['id']==monitored['remote_id']
assert store.inbox('codex:outsider')==[] and store.pending_count('codex:outsider')==0
assert RemoteStore(store,config.connection).acknowledgements()==[]
assert len(store.history(folder=None))==2 # Its two outgoing messages; no monitor row.
assert wakes==['codex:vps','codex:vps']
_,pc_config=select('pc')
assert Relay(pc_config).request('GET','/v1/status?id='+monitored['remote_id'])['status']=='delivered_local'
# Policy removal preserves human history without native pending text.
(root/'outsider.toml').unlink()
os.environ['LLM_BUS_CONFIG']=str(root/'outsider.toml')
assert len(RemoteStore.monitor_history(Store(store.path),repository=None))==1
assert store.inbox('codex:outsider')==[] and store.pending_count('codex:outsider')==0
pc,_=select('pc')
assert pc.inbox('codex:pc')==[] and pc.pending_count('codex:pc')==0
assert len(wakes)==2
print('three-machine policy, report, and monitor replay passed')
`;
    const result = await promisify(execFile)('uv', ['run','python','-c',program,folder,origin], {cwd: new URL('../..',import.meta.url).pathname, timeout:30000});
    expect(result.stdout).toContain('three-machine policy, report, and monitor replay passed');
  } finally { await mf.dispose(); rmSync(folder,{recursive:true,force:true}); }
},40000);
