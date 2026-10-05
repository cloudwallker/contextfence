"""Analyze each client's original E2E sample, including failures; never average percentiles."""
import argparse
import collections
import json
import math
import pathlib


class EvidenceError(ValueError):
    pass


def quantiles(values):
    if not values:
        return {'p50':None,'p95':None,'p99':None,'max':None}
    ordered = sorted(values)
    result = {name:ordered[max(0, math.ceil(len(ordered)*fraction)-1)]
              for name,fraction in [('p50',.50),('p95',.95),('p99',.99)]}
    result['max'] = ordered[-1]
    return result


def summarize(paths, measure_seconds, planned_rps, phase='measure'):
    if measure_seconds <= 0 or planned_rps <= 0:
        raise EvidenceError('Invalid measurement duration or arrival rate')
    latency = []; by_category = collections.defaultdict(list); categories = collections.Counter()
    operations = collections.Counter(); issued = 0; dropped = 0; vus = []; max_vus = []
    valid_categories = {'success','expected_denial','unexpected_failure','unsafe_allow','timeout','transport_failure','client_failure'}
    for path in paths:
        started_by_operation = collections.Counter(); issued_by_operation = collections.Counter()
        completed_by_operation = collections.Counter()
        with pathlib.Path(path).open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip(): continue
                try:
                    point = json.loads(line)
                    if point.get('type') != 'Point': continue
                    metric = point.get('metric'); data = point['data']; tags = data.get('tags') or {}
                    value = data['value']
                    if metric == 'dropped_iterations' and tags.get('scenario',tags.get('phase')) == phase:
                        if value < 0: raise ValueError()
                        dropped += value
                    elif metric == 'vus': vus.append(value)
                    elif metric == 'vus_max': max_vus.append(value)
                    elif (metric == 'cf_business_started' or
                          (metric == 'http_reqs' and tags.get('traffic') == 'business')) and tags.get('phase') == phase:
                        operation = tags['operation']
                        if (operation not in ('read','write') or not isinstance(value,(int,float)) or
                            not math.isfinite(value) or value < 0 or value != int(value)):
                            raise ValueError()
                        counter = started_by_operation if metric == 'cf_business_started' else completed_by_operation
                        counter[operation] += value
                    elif metric == 'cf_e2e_ms' and tags.get('phase') == phase:
                        category = tags['category']
                        if category not in valid_categories or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0:
                            raise ValueError()
                        operation = tags['operation']
                        if operation not in ('read','write') or tags['issued'] not in ('0','1'):
                            raise ValueError()
                        categories[category] += 1; operations[operation] += 1
                        if tags['issued'] == '1':
                            issued_by_operation[operation] += 1
                            issued += 1; latency.append(value); by_category[category].append(value)
                        elif category != 'client_failure': raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise EvidenceError('Malformed raw load evidence') from None
        # A killed final request must not disappear behind the arrival-rate tolerance or another raw file.
        if started_by_operation != issued_by_operation or completed_by_operation != issued_by_operation:
            raise EvidenceError('Incomplete business request samples in raw load evidence')
    if not categories:
        raise EvidenceError('Missing original request measurement samples')
    expected = categories['success'] + categories['expected_denial']
    failures = sum(categories.values()) - expected
    planned = planned_rps * measure_seconds
    # Missed arrivals and client failures invalidate a capacity claim, even if issued requests passed.
    eligible = (categories['unsafe_allow'] == 0 and failures / sum(categories.values()) <= .01 and
                dropped == 0 and categories['client_failure'] == 0 and issued >= planned*.99)
    return {'format_version':1,'phase':phase,'measurement_seconds':measure_seconds,
            'planned_rps':planned_rps,'planned_requests':planned,'issued_requests':issued,
            'issued_rps':issued/measure_seconds,'expected_completed_rps':expected/measure_seconds,
            'categories':dict(sorted(categories.items())),'operations':dict(sorted(operations.items())),
            'unexpected_failure_rate':failures/sum(categories.values()),'unsafe_allow_count':categories['unsafe_allow'],
            'dropped_iterations':dropped,'client_limited':dropped>0 or categories['client_failure']>0,
            'latency_ms':quantiles(latency),'category_latency_ms':{k:quantiles(v) for k,v in sorted(by_category.items())},
            'success_latency_ms':quantiles(by_category['success']),
            'failure_latency_ms':quantiles([value for category,values in by_category.items()
                if category not in ('success','expected_denial') for value in values]),
            'expected_denial_latency_ms':quantiles(by_category['expected_denial']),
            'peak_vus':max(vus,default=None),'allocated_vus_max':max(max_vus,default=None),'capacity_eligible':eligible,
            'percentile_method':'nearest-rank over original per-request E2E samples; timeouts retained'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw',nargs='+'); parser.add_argument('--seconds',type=int,required=True)
    parser.add_argument('--rps',type=int,required=True); parser.add_argument('--phase',default='measure')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        report=summarize(args.raw,args.seconds,args.rps,args.phase)
        pathlib.Path(args.output).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print('PASS: original load samples analyzed; response bodies were not printed')
        return 0
    except (EvidenceError,OSError):
        print('FAIL: raw load evidence is incomplete or invalid')
        return 1


if __name__ == '__main__': raise SystemExit(main())
