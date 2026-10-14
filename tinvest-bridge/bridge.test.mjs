import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {once} from 'node:events';
import {createBridge, createUpstream, METHODS} from './server.mjs';

const KEY = 'test-only-bridge-key-not-a-real-secret-123456789';
const TOKEN = 't.test_only_token_123456789012345678901234567890';

async function fixture(run, options = {}) {
  const requests = [];
  const server = createBridge({key: KEY, upstream: async (...args) => {
    requests.push(args);
    return options.result || {status: 200, text: JSON.stringify({sequence: requests.length})};
  }, ...options});
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = 'http://127.0.0.1:' + server.address().port;
  const call = (method = 'UsersService/GetAccounts', body = {}, extra = {}) => fetch(base + '/api/' + method,
    {method: 'POST', headers: {'x-bridge-key': KEY, 'x-t-invest-token': TOKEN,
      'content-type': 'application/json', ...extra}, body: JSON.stringify(body)});
  try {await run({base, call, requests});}
  finally {server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
}

test('health is public and does not contact the broker', () => fixture(async ({base, requests}) => {
  assert.deepEqual(await (await fetch(base + '/health')).json(), {ok: true});
  assert.equal(requests.length, 0);
}));

test('account data requires a strong bridge key', () => fixture(async ({call, requests}) => {
  assert.equal((await call(undefined, {}, {'x-bridge-key': 'wrong'})).status, 401);
  assert.equal(requests.length, 0);
}));

test('only explicitly listed read methods can reach the broker', () => fixture(async ({call, requests}) => {
  for (const method of ['OrdersService/PostOrder', 'UsersService/PayIn',
    'UsersService/CurrencyTransfer', 'https://attacker.invalid']) {
    assert.equal((await call(method)).status, 403);
  }
  assert.equal(requests.length, 0);
  assert.equal(METHODS.has('UsersService/GetAccounts'), true);
}));

test('GET and credentials in URL are not accepted', () => fixture(async ({base, requests}) => {
  const response = await fetch(base + '/api/UsersService/GetAccounts?token=not-secret',
    {headers: {'x-bridge-key': KEY}});
  assert.equal(response.status, 405);
  assert.equal(requests.length, 0);
}));

test('content type and token format are validated', () => fixture(async ({call, requests}) => {
  assert.equal((await call(undefined, {}, {'content-type': 'text/plain'})).status, 415);
  assert.equal((await call(undefined, {}, {'x-t-invest-token': ''})).status, 400);
  assert.equal(requests.length, 0);
}));

test('JSON object and bounded request body required', () => fixture(async ({call, requests}) => {
  assert.equal((await call(undefined, [])).status, 400);
  assert.equal((await call(undefined, {oversized: 'a'.repeat(33 * 1024)})).status, 413);
  assert.equal(requests.length, 0);
}));

test('cache avoids duplicate broker calls and separates tokens', () => fixture(async ({call, requests}) => {
  assert.equal((await call()).status, 200);
  assert.equal((await call()).status, 200);
  assert.equal(requests.length, 1);
  await call(undefined, {}, {'x-t-invest-token': TOKEN + 'another'});
  assert.equal(requests.length, 2);
}));

test('cache expires; financial data does not become permanently stale', async () => {
  let clock = 0;
  await fixture(async ({call, requests}) => {
    await call('OperationsService/GetPortfolio');
    clock = 30001;
    await call('OperationsService/GetPortfolio');
    assert.equal(requests.length, 2);
  }, {now: () => clock});
});

test('upstream errors and token-like text are not exposed', () => fixture(async ({call}) => {
  const response = await call();
  assert.equal(response.status, 401);
  assert.equal((await response.text()).includes(TOKEN), false);
}, {result: {status: 401, text: TOKEN}}));

test('HTML cannot be mistaken for a successful financial response', () => fixture(async ({call}) => {
  assert.equal((await call()).status, 502);
}, {result: {status: 200, text: '<html>Starting server</html>'}}));

test('connection failures are sanitized', () => fixture(async ({call}) => {
  const response = await call();
  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), {error: 'BROKER_CONNECTION_FAILED'});
}, {upstream: async () => {throw new Error(TOKEN);}}));

test('rate limit caps the number of actual broker calls', () => fixture(async ({call, requests}) => {
  for (let i = 0; i < 90; i++) assert.equal((await call(undefined, {fixture: i})).status, 200);
  assert.equal((await call(undefined, {fixture: 90})).status, 429);
  assert.equal(requests.length, 90);
}));

test('missing bridge key and missing/invalid CA fail closed', () => {
  assert.throws(() => createBridge({key: 'short', upstream: async () => {}}));
  assert.throws(() => createUpstream(''));
  assert.throws(() => createUpstream('-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----'));
});

test('official CA bundle parses without changing global TLS trust', () => {
  const oldValue = process.env.NODE_TLS_REJECT_UNAUTHORIZED;
  const pem = readFileSync(new URL('./russian-trusted-ca.pem', import.meta.url), 'utf8');
  assert.equal(typeof createUpstream(pem), 'function');
  assert.equal(process.env.NODE_TLS_REJECT_UNAUTHORIZED, oldValue);
});
