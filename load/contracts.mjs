// Shared pure response contract: imports in k6 and Node behavior tests.
function same(a, b) {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length &&
      a.every((value, index) => same(value, b[index]));
  }
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key =>
    Object.prototype.hasOwnProperty.call(b, key) && same(a[key], b[key]));
}
function versions(values) {
  return (values || []).slice().sort((a, b) => a.source_id.localeCompare(b.source_id));
}
export function classify(expected, response) {
  if (response.status === 0) return response.error_code === 1050 || response.error_code === 1211 ? 'timeout' : 'transport_failure';
  const body = response.body;
  if (response.status >= 400 && body && (body.items || []).length) return 'unsafe_allow';
  if (expected.status >= 400 && (response.status < 400 || (body && (body.items || []).length))) return 'unsafe_allow';
  if (!body || response.status !== expected.status) return 'unexpected_failure';
  if (expected.operation === 'write') {
    return body.source_id === expected.source_id && body.sequence === expected.sequence &&
      ['APPLIED', 'IGNORED_STALE'].includes(body.outcome) && body.content_version === expected.content_version &&
      body.auth_epoch === expected.auth_epoch ? 'success' : 'unexpected_failure';
  }
  const receipt = body.receipt;
  if (body.code !== expected.code || body.status !== expected.status || !receipt || receipt.decision !== expected.code ||
      !same(receipt.context_ids, expected.context_ids) || !same(versions(receipt.sources), versions(expected.source_versions))) return 'unexpected_failure';
  if (expected.status === 200) {
    return same(body.items, expected.items) && same(receipt.reasons, []) ? 'success' : 'unexpected_failure';
  }
  const text = JSON.stringify(body);
  if ((expected.forbidden_contents || []).some(marker => text.includes(marker))) return 'unsafe_allow';
  const reasons = receipt.reasons || [];
  return [403, 409].includes(expected.status) && same(body.items, []) && reasons.length === expected.context_ids.length &&
    reasons.every((r, i) => r.context_id === expected.context_ids[i] && r.code === expected.code) ? 'expected_denial' : 'unexpected_failure';
}
