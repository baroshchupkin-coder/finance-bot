import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import {createHash, timingSafeEqual, X509Certificate} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';

export const API_HOST = 'invest-public-api.tbank.ru';
const PREFIX = '/rest/tinkoff.public.invest.api.contract.v1.';
export const METHODS = new Set([
  'UsersService/GetAccounts',
  'OperationsService/GetPortfolio',
  'OperationsService/GetPositions',
  'OperationsService/GetOperationsByCursor',
  'InstrumentsService/GetInstrumentBy',
  'InstrumentsService/GetBondCoupons',
  'InstrumentsService/GetDividends',
  'MarketDataService/GetLastPrices',
  'MarketDataService/GetCandles',
]);
const MAX_INPUT = 32 * 1024;
const MAX_OUTPUT = 2 * 1024 * 1024;
const MAX_CACHE_BYTES = 8 * 1024 * 1024;
const digest = value => createHash('sha256').update(value).digest();

export function createUpstream(caPem) {
  const certificates = caPem.match(/-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g);
  if (!certificates?.length) throw new Error('A verified CA certificate is required');
  const currentCertificates = certificates.filter(pem => {
    const cert = new X509Certificate(pem);
    if (!cert.ca) throw new Error('Invalid CA');
    return Date.parse(cert.validFrom) <= Date.now() && Date.parse(cert.validTo) > Date.now();
  });
  if (!currentCertificates.length) throw new Error('No current CA certificate');
  // The additional CA is limited to this agent and the fixed broker hostname.
  // Neither the operating system nor unrelated application HTTPS clients change.
  const agent = new https.Agent({ca: [...tls.rootCertificates, ...currentCertificates],
    rejectUnauthorized: true, keepAlive: true, maxSockets: 4});
  return function upstream(method, body, token) {
    return new Promise((resolve, reject) => {
      const payload = JSON.stringify(body);
      const request = https.request({hostname: API_HOST, servername: API_HOST,
        port: 443, path: PREFIX + method, method: 'POST', agent,
        rejectUnauthorized: true, checkServerIdentity: tls.checkServerIdentity,
        headers: {'authorization': 'Bearer ' + token, 'content-type': 'application/json',
          'content-length': Buffer.byteLength(payload), 'x-app-name': 'investment-sheet-readonly'}}, response => {
        const chunks = [];
        let bytes = 0;
        response.on('data', chunk => {
          bytes += chunk.length;
          if (bytes > MAX_OUTPUT) response.destroy(new Error('UPSTREAM_TOO_LARGE'));
          else chunks.push(chunk);
        });
        response.on('error', reject);
        response.on('end', () => resolve({status: response.statusCode,
          text: Buffer.concat(chunks).toString('utf8')}));
      });
      request.setTimeout(18000, () => request.destroy(new Error('UPSTREAM_TIMEOUT')));
      request.on('error', reject);
      request.end(payload);
    });
  };
}

function reply(res, status, data) {
  res.writeHead(status, {'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store', 'x-content-type-options': 'nosniff'});
  res.end(typeof data === 'string' ? data : JSON.stringify(data));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let bytes = 0;
    const chunks = [];
    req.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > MAX_INPUT) {reject(new Error('INPUT_TOO_LARGE')); req.resume();}
      else chunks.push(chunk);
    });
    req.on('error', reject);
    req.on('end', () => {
      try {
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error();
        resolve(body);
      } catch {reject(new Error('INVALID_JSON'));}
    });
  });
}

export function createBridge({key, upstream, now = Date.now}) {
  if (typeof key !== 'string' || key.length < 32) throw new Error('Strong bridge key required');
  const keyHash = digest(key);
  const cache = new Map();
  const inflight = new Map();
  let cacheBytes = 0;
  let windowStart = now();
  let calls = 0;

  const server = http.createServer(async (req, res) => {
    if (req.url === '/health' && (req.method === 'GET' || req.method === 'HEAD')) {
      return reply(res, 200, {ok: true}); // Does not call the broker or disclose account data.
    }
    const provided = req.headers['x-bridge-key'];
    if (typeof provided !== 'string' || !timingSafeEqual(digest(provided), keyHash)) {
      return reply(res, 401, {error: 'UNAUTHORIZED'});
    }
    if (req.method !== 'POST') return reply(res, 405, {error: 'POST_REQUIRED'});
    const method = req.url?.startsWith('/api/') ? req.url.slice(5) : '';
    if (!METHODS.has(method)) return reply(res, 403, {error: 'READ_ONLY_METHOD_REQUIRED'});
    if (!/^application\/json(?:;|$)/i.test(req.headers['content-type'] || '')) {
      return reply(res, 415, {error: 'JSON_REQUIRED'});
    }
    const token = req.headers['x-t-invest-token'];
    if (typeof token !== 'string' || !/^t\.[A-Za-z0-9_-]{20,300}$/.test(token)) {
      return reply(res, 400, {error: 'TOKEN_REQUIRED'});
    }
    try {
      const body = await readBody(req);
      const payload = JSON.stringify(body);
      const cacheKey = digest(token + '\0' + method + '\0' + payload).toString('hex');
      const cached = cache.get(cacheKey);
      if (cached && cached.expires > now()) return reply(res, 200, cached.text);
      if (cached) {cache.delete(cacheKey); cacheBytes -= cached.bytes;}
      if (now() - windowStart >= 60000) {windowStart = now(); calls = 0;}
      if (!inflight.has(cacheKey)) {
        if (++calls > 90) return reply(res, 429, {error: 'RATE_LIMIT'});
        const pending = Promise.resolve().then(() => upstream(method, body, token));
        inflight.set(cacheKey, pending);
        // Release the token-bearing promise on either outcome, without keeping
        // fulfilled requests around or attaching handlers to unrelated calls.
        pending.then(() => inflight.delete(cacheKey), () => inflight.delete(cacheKey));
      }
      const result = await inflight.get(cacheKey);
      // No request headers, tokens, payloads, account identifiers or upstream errors are logged.
      if (result.status !== 200) {
        return reply(res, result.status >= 400 && result.status <= 599 ? result.status : 502,
          {error: 'BROKER_REQUEST_FAILED', upstreamStatus: result.status});
      }
      JSON.parse(result.text); // Do not treat an HTML startup/proxy page as financial data.
      const bytes = Buffer.byteLength(result.text);
      if (bytes > MAX_OUTPUT) return reply(res, 502, {error: 'UPSTREAM_TOO_LARGE'});
      if (!cache.has(cacheKey)) {
        while (cache.size >= 300 || cacheBytes + bytes > MAX_CACHE_BYTES) {
          const oldestKey = cache.keys().next().value;
          if (oldestKey === undefined) break;
          cacheBytes -= cache.get(oldestKey).bytes;
          cache.delete(oldestKey);
        }
        const ttl = method.startsWith('InstrumentsService/') ? 6 * 60 * 60 * 1000 : 30000;
        cache.set(cacheKey, {text: result.text, bytes, expires: now() + ttl});
        cacheBytes += bytes;
      }
      return reply(res, 200, result.text);
    } catch (error) {
      const status = error.message === 'INPUT_TOO_LARGE' ? 413 : error.message === 'INVALID_JSON' ? 400 : 502;
      return reply(res, status, {error: status === 502 ? 'BROKER_CONNECTION_FAILED' : error.message});
    }
  });
  server.requestTimeout = 25000;
  server.headersTimeout = 10000;
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const pem = readFileSync(new URL('./russian-trusted-ca.pem', import.meta.url), 'utf8');
  const server = createBridge({key: process.env.BRIDGE_KEY, upstream: createUpstream(pem)});
  server.listen(Number(process.env.PORT || 10000), '0.0.0.0', () => console.log('Read-only bridge ready'));
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => server.close());
}
