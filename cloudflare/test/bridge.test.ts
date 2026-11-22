/** Real HTTP/Worker/D1/Queue/Python integration. All machines and wakes are isolated. */
import { Miniflare } from 'miniflare';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { expect, it } from 'vitest';
import { tokenHash } from '../src/auth';

it('delivers across two Python mailboxes through HTTP and Queue, then synchronizes recipient ack', async () => {
  const mf = new Miniflare({modules:true, scriptPath:'dist/index.js', compatibilityDate:'2026-08-01',
    d1Databases:['DB'], queueProducers:{DELIVERY:'delivery'}, queueConsumers:{delivery:{maxBatchSize:1,maxBatchTimeout:0}}});
  const folder = mkdtempSync(join(tmpdir(), 'llm-bus-e2e-'));
  try {
    const db = await mf.getD1Database('DB');
    const migration = ['0001_bus.sql','0002_reports.sql','0003_reply_to.sql','0004_coordination.sql','0005_session_labels.sql','0006_coordination_availability.sql','0007_coordination_approvals.sql','0008_message_retention.sql','0009_retained_reply_identity.sql'].map(name=>readFileSync(new URL(`../migrations/${name}`, import.meta.url), 'utf8')).join(';');
    for (const statement of migration.split(';').filter(s=>s.trim())) await db.prepare(statement).run();
    for (const machine of ['a','b']) {
      await db.prepare('INSERT INTO machines(id,token_hash) VALUES (?,?)').bind(machine,await tokenHash(machine.repeat(43))).run();
      await db.prepare('INSERT INTO repo_grants VALUES (?,?)').bind(machine,'xyz').run();
    }
    const origin = (await mf.ready).origin;
    const program = readFileSync(new URL('./scenarios/delivery.py', import.meta.url), 'utf8');
    const result = await promisify(execFile)('uv', ['run','python','-u','-c',program,folder,origin], {cwd: new URL('../..',import.meta.url).pathname, timeout:30000}).catch(error => {
      throw new Error(`Python scenario failed: killed=${error.killed}, code=${error.code}\n${error.stdout}\n${error.stderr}`);
    });
    expect(result.stdout).toContain('cross-machine delivery and acknowledgement passed');
    expect(result.stdout).toContain('owner-bound join and automatic assignment passed');
    expect(result.stdout).toContain('versioned Codex role withdrawal and recovery passed');
  } finally { await mf.dispose(); rmSync(folder,{recursive:true,force:true}); }
},40000);

it('enforces independent PC/VPS/outsider policies and keeps monitor replay outside native delivery', async () => {
  const mf = new Miniflare({modules:true, scriptPath:'dist/index.js', compatibilityDate:'2026-08-01',
    d1Databases:['DB'], queueProducers:{DELIVERY:'delivery'}, queueConsumers:{delivery:{maxBatchSize:1,maxBatchTimeout:0}}});
  const folder = mkdtempSync(join(tmpdir(), 'llm-bus-policy-e2e-'));
  try {
    const db = await mf.getD1Database('DB');
    const migration = ['0001_bus.sql','0002_reports.sql','0003_reply_to.sql','0004_coordination.sql','0005_session_labels.sql','0006_coordination_availability.sql','0007_coordination_approvals.sql','0008_message_retention.sql','0009_retained_reply_identity.sql'].map(name=>readFileSync(new URL(`../migrations/${name}`, import.meta.url), 'utf8')).join(';');
    for (const statement of migration.split(';').filter(s=>s.trim())) await db.prepare(statement).run();
    for (const machine of ['pc','vps','outsider']) {
      await db.prepare('INSERT INTO machines(id,token_hash) VALUES (?,?)').bind(machine,await tokenHash(machine[0].repeat(43))).run();
      await db.prepare('INSERT INTO repo_grants VALUES (?,?)').bind(machine,'xyz').run();
    }
    const origin = (await mf.ready).origin;
    const program = readFileSync(new URL('./scenarios/policy.py', import.meta.url), 'utf8');
    const result = await promisify(execFile)('uv', ['run','python','-c',program,folder,origin], {cwd: new URL('../..',import.meta.url).pathname, timeout:30000});
    expect(result.stdout).toContain('three-machine policy, report, and monitor replay passed');
  } finally { await mf.dispose(); rmSync(folder,{recursive:true,force:true}); }
},40000);
