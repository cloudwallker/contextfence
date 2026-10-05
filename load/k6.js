import http from 'k6/http';
import exec from 'k6/execution';
import { Trend, Counter } from 'k6/metrics';
import { classify } from './contracts.mjs';
import { arrivalOptions, selectRequest } from './protocol.mjs';

const e2e = new Trend('cf_e2e_ms');
const businessStarted = new Counter('cf_business_started');
const auxiliary = new Counter('cf_fixture_requests');
const opts = { rps: Number(__ENV.CF_RPS), warmup: Number(__ENV.CF_WARMUP), measure: Number(__ENV.CF_MEASURE),
  vus: Number(__ENV.CF_VUS), maxVus: Number(__ENV.CF_MAX_VUS) };
export const options = arrivalOptions(opts);
if (!['http://127.0.0.1:58090', 'http://127.0.0.1:58095'].includes(__ENV.CF_BASE) || !/^http:\/\/127\.0\.0\.1:\d+$/.test(__ENV.CF_FIXTURE_URL || '')) {
  throw new Error('Only the local HAProxy and fixture service are permitted');
}
let snapshot, cachedAt = 0;
function fixture(path, payload) {
  const params = { headers: { 'Content-Type': 'application/json', 'X-Fixture-Capability': __ENV.CF_FIXTURE_CAPABILITY },
    timeout: '3s', tags: { traffic: 'auxiliary', name: 'fixture' } };
  const reply = payload === undefined ? http.get(__ENV.CF_FIXTURE_URL + path, params) :
    http.post(__ENV.CF_FIXTURE_URL + path, JSON.stringify(payload), params);
  auxiliary.add(1, { phase: exec.scenario.name, route: path });
  if (reply.status !== 200) throw new Error('Fixture service unavailable');
  return reply.json();
}
export function business() {
  const phase = exec.scenario.name;
  let operation = 'read', issued = '0', category = 'client_failure', elapsed = 0;
  try {
    if (!snapshot || Date.now() - cachedAt >= 1000) { snapshot = fixture('/snapshot'); cachedAt = Date.now(); }
    const choice = selectRequest(__ENV.CF_SCENARIO, exec.scenario.iterationInTest,
      snapshot.tenants.length, snapshot.tenants[0].sources.length);
    const tenant = snapshot.tenants[choice.tenant];
    const row = tenant.sources[choice.source];
    operation = choice.operation;
    let expected = row.expected, payload = { context_ids: row.context_ids }, path = '/v1/contexts/assemble';
    if (operation === 'write') {
      const reservation = fixture('/reserve', { tenant: tenant.tenant, index: row.index });
      expected = reservation.expected; payload = reservation.event; path = '/v1/source-events';
    }
    const params = { headers: { 'Content-Type': 'application/json',
      Authorization: `Bearer ${operation === 'write' ? tenant.writer_token : tenant.reader_token}` },
      timeout: '30s', tags: { traffic: 'business', name: operation, phase, operation } };
    const requestBody = JSON.stringify(payload);
    const started = Date.now(); issued = '1';
    try {
      businessStarted.add(1, { phase, operation });
      const reply = http.post(__ENV.CF_BASE + path, requestBody, params);
      elapsed = Date.now() - started;
      let body = null; try { body = reply.json(); } catch (_) {}
      category = classify(expected, { status: reply.status, body, error_code: reply.error_code });
    } catch (_) { elapsed = Date.now() - started; category = 'transport_failure'; }
  } catch (_) { category = 'client_failure'; }
  // Original wall-clock E2E per issued business request: DNS/queue/connect/body and timeouts included.
  e2e.add(elapsed, { phase, category, operation, issued });
}
