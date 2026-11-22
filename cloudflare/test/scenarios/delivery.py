import os,sys,time
from pathlib import Path
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.outgoing import enqueue_remote
from llm_bus.policy.config import Config, Repository, PeerPolicy
from llm_bus.remote.store import RemoteStore
from llm_bus.reports.store import Reports
from llm_bus.mailbox.store import Store
from llm_bus.mailbox.models import SenderContext
from llm_bus.routing import routing_scope
import llm_bus.wake.dispatch
import llm_bus.wake.adapters
def forbid_native(*args,**kwargs):
    raise AssertionError('isolated integration must not execute native adapters')
llm_bus.wake.adapters._wake_codex=forbid_native
llm_bus.wake.adapters._wake_claude=forbid_native
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
llm_bus.wake.dispatch.wake=wake
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
# Receiver owners bind the same project once; no per-worker grant/target exchange.
print('ordinary delivery/reply checks passed; starting coordination')
from llm_bus.coordination import cli
from llm_bus.coordination.delivery import outgoing
workspace=root/'memory'; workspace.mkdir(); (workspace/'.beads').mkdir()
common={'project':'demo','objective':'review','beads_workspace':str(workspace),'memory_ticket':'memory-1'}
select('a')
created=cli.run(stores['a'],{'command':'coordinate',**common},'codex:sender',str(repo))
with stores['a'].transaction() as db:
    project_id,generation=db.execute('SELECT project_id,generation FROM coordination_projects').fetchone()
select('b')
cli.run(stores['b'],{'command':'setup',**common,'coordinator':'a/xyz/codex:sender','project_id':project_id,'generation':generation},'claude:recipient',str(repo))
joined=cli.run(stores['b'],{'command':'join','project':'demo'},'claude:recipient',str(repo))
print('owner-bound join queued')
assert bridge(select('b'),configs['b']).sync()['errors']==0
for attempt in range(50):
    assert bridge(select('a'),configs['a']).sync()['errors']==0
    with stores['a'].transaction() as db:
        target=db.execute('SELECT grant_id FROM coordination_targets WHERE recipient=?',('b/xyz/claude:recipient',)).fetchone()
    if target: break
    time.sleep(.05)
assert target, 'availability did not create target'
print('availability target received')
with stores['a'].transaction() as db:
    direction=outgoing(db,sender='codex:sender',scope=configs['a'].repositories[0].scope,label='demo',recipient='b/xyz/claude:recipient',operation='assign')
assert direction.grant_id==target[0]
assignment=enqueue_remote(stores['a'],configs['a'],SenderContext('codex','sender',str(repo)),'b/xyz/claude:recipient','assigned automatically',direction=direction)
assert bridge(select('a'),configs['a']).sync()['errors']==0
for attempt in range(50):
    assert bridge(select('b'),configs['b']).sync()['errors']==0
    assigned=[item for item in stores['b'].inbox('claude:recipient') if item['body']=='assigned automatically']
    if assigned: break
    time.sleep(.05)
assert len(assigned)==1
assert assigned[0]['coordination']['verified'] is True
select('b')
cli.run(stores['b'],{'command':'leave','project':'demo'},'claude:recipient',str(repo))
assert not [item for item in stores['b'].inbox('claude:recipient') if item['body']=='assigned automatically']
print('cross-machine delivery and acknowledgement passed')
print('owner-bound join and automatic assignment passed')

# Repair a withdrawal that a legacy client cached as sent at the old revision.
import json
from unittest.mock import patch
from llm_bus.remote.discovery import publish_sessions

role_address = stores['a'].register('codex', 'role-check', str(repo))
key = f'xyz/{role_address}'
publisher = bridge(Store(stores['a'].path), configs['a'])
observer = bridge(stores['b'], configs['b']).relay
with patch('llm_bus.remote.discovery.is_subagent', return_value=False):
    assert publish_sessions(publisher) == 0
original = json.loads(publisher.remote.publication_payloads()[key])
withdrawal = {**original, 'ended': True}
publisher.relay.request('POST', '/v1/sessions', withdrawal)
publisher.remote.record_publication(key, json.dumps(withdrawal, sort_keys=True))
with stores['a'].transaction() as db:
    db.execute("UPDATE session_publications SET payload = json_remove(payload, '$.native_child') "
               "WHERE address = ?", (role_address,))


def advertised():
    return [row for row in observer.request('GET', '/v1/sessions?repo_id=xyz')
            if row['native_address'] == role_address]


assert advertised(), 'same-revision withdrawal should have been ignored by the relay'
with patch('llm_bus.remote.discovery.is_subagent', side_effect=lambda address: address == role_address):
    publisher = bridge(Store(stores['a'].path), configs['a'])
    assert publish_sessions(publisher) == 0
assert not advertised(), 'the upgraded bridge must withdraw the legacy child advertisement'
publisher.relay.request('POST', '/v1/sessions', original)
assert not advertised(), 'a delayed older revision must not resurrect the advertisement'
with patch('llm_bus.remote.discovery.is_subagent', return_value=False):
    assert publish_sessions(publisher) == 0
assert advertised()[0]['revision'] > original['revision']
print('versioned Codex role withdrawal and recovery passed')
