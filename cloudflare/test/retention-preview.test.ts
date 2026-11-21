import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';
import { bindings, send } from './relay-fixture';

const sql = readFileSync(new URL('../retention-preview.sql', import.meta.url), 'utf8');

it('previews legacy and migrated relay rows without bodies or mutation, and flags invalid ages', async () => {
  const old = await send(), expired = await send(), fresh = await send();
  const ages = [new Date(Date.now()-15*86400_000).toISOString(), new Date(Date.now()-3*86400_000).toISOString()];
  await bindings.DB.prepare('UPDATE messages SET sent_at=? WHERE id=?').bind(ages[0],old.message.id).run();
  await bindings.DB.prepare('UPDATE messages SET sent_at=? WHERE id=?').bind(ages[1],expired.message.id).run();
  const before = (await bindings.DB.prepare('SELECT * FROM messages ORDER BY sequence').all()).results;
  const result = await bindings.DB.prepare(sql).first();
  expect(result).toMatchObject({invalid_timestamps:0, expired_delivery_candidates:2,
    oldest_expired_delivery_at:ages[0], deletion_candidates:1, oldest_eligible_at:ages[0]});
  expect(JSON.stringify(result)).not.toContain('hello');
  expect((await bindings.DB.prepare('SELECT * FROM messages ORDER BY sequence').all()).results).toEqual(before);
  await bindings.DB.batch([
    'DROP INDEX messages_retention', 'ALTER TABLE messages DROP COLUMN retention_at',
    'ALTER TABLE messages DROP COLUMN report_expires_at',
  ].map(statement=>bindings.DB.prepare(statement)));
  expect(await bindings.DB.prepare(sql).first()).toMatchObject({deletion_candidates:1,invalid_timestamps:0});
  await bindings.DB.prepare('UPDATE messages SET sent_at=? WHERE id=?').bind('private invalid timestamp',fresh.message.id).run();
  const invalid = await bindings.DB.prepare(sql).first();
  expect(invalid).toMatchObject({invalid_timestamps:1});
  expect(JSON.stringify(invalid)).not.toContain('private');
});
