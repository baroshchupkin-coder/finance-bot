import {readFileSync} from 'node:fs';
import {createUpstream} from './server.mjs';

// An invalid placeholder is deliberate: this is a TLS test, not a user account query.
const pem = readFileSync(new URL('./russian-trusted-ca.pem', import.meta.url), 'utf8');
const request = createUpstream(pem);
try {
  const response = await request('UsersService/GetAccounts', {}, 'not-a-real-token');
  if (response.status !== 401) throw new Error('Unexpected unauthenticated response: ' + response.status);
  console.log(JSON.stringify({tlsValidated: true, brokerReached: true, unauthenticatedStatus: 401}));
} catch (error) {
  console.error(JSON.stringify({tlsValidated: false, error: error.code || error.message}));
  process.exitCode = 1;
}
