// Pure protocol shared by Node behavior checks and k6.
export function arrivalOptions({ rps, warmup, measure, vus, maxVus }) {
  if (![rps, measure, vus, maxVus].every(n => Number.isInteger(n) && n > 0) ||
      !Number.isInteger(warmup) || warmup < 0 || maxVus < vus) throw new Error('Invalid arrival protocol');
  const make = (duration, startTime, phase) => ({ executor: 'constant-arrival-rate',
    exec: 'business', rate: rps, timeUnit: '1s', duration: `${duration}s`, startTime: `${startTime}s`,
    preAllocatedVUs: vus, maxVUs: maxVus, gracefulStop: '40s', tags: { phase } });
  const scenarios = { measure: make(measure, warmup, 'measure') };
  if (warmup) scenarios.warmup = make(warmup, 0, 'warmup');
  return { scenarios, discardResponseBodies: false, systemTags: ['status', 'method', 'name', 'scenario', 'error_code'],
    summaryTrendStats: ['p(50)', 'p(95)', 'p(99)', 'max'] };
}

export function selectRequest(scenario, iteration, tenantCount, sourceCount) {
  if (!['cold', 'hot', 'single', 'multi'].includes(scenario) || tenantCount < 1 || sourceCount < 1 ||
      (scenario === 'multi' && tenantCount < 2)) throw new Error('Invalid fixture workload');
  const tenant = scenario === 'multi' ? iteration % tenantCount : 0;
  const localIteration = scenario === 'multi' ? Math.floor(iteration / tenantCount) : iteration;
  const operation = ['single', 'multi'].includes(scenario) && localIteration % 10 === 9 ? 'write' : 'read';
  // Single-tenant writes/readers all contend on its tenant_guard. Hot reads share one batch.
  return { tenant, source: scenario === 'hot' || scenario === 'cold' ? 0 : localIteration % sourceCount, operation };
}
