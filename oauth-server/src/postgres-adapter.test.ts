import assert from "node:assert/strict";
import { createHmac, randomBytes, randomUUID } from "node:crypto";
import test from "node:test";

import { Pool } from "pg";

import {
  createPostgresAdapter,
  initializePostgresAdapter,
} from "./postgres-adapter.js";

const databaseUrl = process.env.TEST_DATABASE_URL;

test("grant management lists safely and revokes only owned grants", { skip: !databaseUrl }, async () => {
  const base = new Pool({ connectionString: databaseUrl });
  const schema = `adapter_test_${randomUUID().replaceAll("-", "")}`;
  await base.query(`CREATE SCHEMA "${schema}"`);
  const scopedUrl = new URL(databaseUrl!);
  scopedUrl.searchParams.set("options", `-c search_path=${schema}`);
  const pool = new Pool({ connectionString: scopedUrl.href });
  try {
    await initializePostgresAdapter(pool);
    const secret = randomBytes(32);
    const adapter = createPostgresAdapter(pool, { secret });
    const grants = adapter("Grant");
    const tokens = adapter("AccessToken");
    const owner = "018f2f5e-7b3c-7abc-8def-0123456789ab";
    const other = randomUUID();
    const rawGrant = randomUUID();
    const otherGrant = randomUUID();
    const now = Math.floor(Date.now() / 1000);
    await grants.upsert(rawGrant, { jti: rawGrant, accountId: owner, clientId: "client-a", iat: now, exp: now + 3600 });
    const expiredGrant = randomUUID();
    await grants.upsert(expiredGrant, { jti: expiredGrant, accountId: owner, clientId: "expired", exp: now - 1 });
    await grants.upsert(otherGrant, { jti: otherGrant, accountId: other, clientId: "client-b", iat: 11, exp: 21 });
    await tokens.upsert("token-a", { jti: "token-a", clientId: "client-a", accountId: owner, grantId: rawGrant }, 3600);

    const listed = await adapter.listAuthorizedGrants(owner);
    assert.equal(listed.length, 1);
    assert.deepEqual(Object.keys(listed[0]!).sort(), ["clientId", "expiresAt", "handle", "issuedAt"]);
    assert.equal(listed[0]!.clientId, "client-a");
    const expiredHandle = createHmac("sha256", secret).update("cookops:oidc-adapter:v1\0grant\0\0").update(expiredGrant).digest("hex");
    assert.equal(await adapter.revokeGrant(owner, expiredHandle), false);
    assert.notEqual(listed[0]!.handle, rawGrant);
    const safe = JSON.stringify(listed);
    assert.equal(safe.includes(rawGrant), false);
    assert.equal(safe.includes("token-a"), false);
    assert.equal(safe.includes("payload"), false);
    assert.equal(safe.includes("jti"), false);
    assert.equal(await adapter.revokeGrant(owner, "bad"), false);
    assert.deepEqual(await adapter.listAuthorizedGrants("not-a-uuid"), []);
    assert.equal(await adapter.revokeGrant("not-a-uuid", listed[0]!.handle), false);
    assert.equal(await adapter.revokeGrant(other, listed[0]!.handle), false);
    assert.ok(await grants.find(rawGrant));
    assert.equal(await adapter.revokeGrant(owner, listed[0]!.handle), true);
    assert.equal(await grants.find(rawGrant), undefined);
    assert.equal(await tokens.find("token-a"), undefined);
    assert.deepEqual(await adapter.listAuthorizedGrants(owner), []);

    const legacyGrant = randomUUID();
    await tokens.upsert("token-legacy", { jti: "token-legacy", clientId: "client-a", accountId: owner, grantId: legacyGrant }, 3600);
    await tokens.revokeByGrantId!(legacyGrant);
    assert.equal(await tokens.find("token-legacy"), undefined);
  } finally {
    await pool.end();
    await base.query(`DROP SCHEMA "${schema}" CASCADE`);
    await base.end();
  }
});
