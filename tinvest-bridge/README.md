# T-Invest read-only bridge

Standalone Node.js 22 service. It is independent of the finance bot in the repository root.

Deploy only this directory (`tinvest-bridge`) as a free Render Web Service. Build: `node --check server.mjs`; start: `npm start`; health check: `/health`.

Set a random `BRIDGE_KEY` of at least 32 characters in the service environment and the corresponding Apps Script properties. Never commit keys or broker tokens. The broker token is supplied per request in `x-t-invest-token`, not stored as a service environment variable.

Only explicitly listed read-only broker methods are proxied. Certificates and hostname are validated against the official CA bundle, scoped to the fixed T-Bank upstream. Request payloads, credentials and financial responses are not logged.

Run local checks with `npm test`. `node verify-upstream.mjs` tests TLS with a deliberately invalid placeholder token; HTTP 401 means the bank was reached with certificate validation enabled.

Free hosting can sleep or restart and has no availability guarantee. Financial records and synchronization cursors must remain in Google Sheets / Script Properties, not in the service filesystem or temporary cache.
