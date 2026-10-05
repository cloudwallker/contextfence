"""Behavior checks for arrival-rate load fixtures and unaveraged raw evidence."""
import importlib
import os
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def module(test, name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        test.fail('Missing load behavior: ' + name)


class ResponseClassificationTest(unittest.TestCase):
    def classify(self, cases):
        program = ("import { classify } from './load/contracts.mjs';import fs from 'node:fs';" +
            "console.log(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8'))" +
            '.map(c => classify(c.expected,c.response))));')
        result = subprocess.run([os.environ.get('NODE_BINARY','node'), '--input-type=module', '-e', program], cwd=ROOT,
                                input=json.dumps(cases),capture_output=True, text=True)
        self.assertEqual(0, result.returncode, 'Response classifier must execute successfully')
        return json.loads(result.stdout)

    def test_status_alone_does_not_turn_unexpected_denial_into_success(self):
        expected = {'status': 403, 'code': 'SOURCE_ACCESS_DENIED', 'context_ids': ['c1'],
                    'source_versions': [{'source_id': 's1', 'content_version': 1, 'auth_epoch': 1}],
                    'forbidden_contents': ['synthetic-secret']}
        body = {'status':403,'code':'SOURCE_ACCESS_DENIED','items':[],
                'receipt':{'decision':'SOURCE_ACCESS_DENIED','context_ids':['c1'],
                           'sources':[{'source_id':'s1','content_version':1,'auth_epoch':1}],
                           'reasons':[{'context_id':'c1','code':'SOURCE_ACCESS_DENIED'}]}}
        bad = dict(body, code='CONTEXT_STALE')
        leaked = dict(body, items=[{'id':'c1','content':'synthetic-secret'}])
        results = self.classify([
            {'expected':expected,'response':{'status':403,'body':body}},
            {'expected':expected,'response':{'status':403,'body':bad}},
            {'expected':expected,'response':{'status':403,'body':leaked}},
            {'expected':expected,'response':{'status':200,'body':{'code':'ALLOWED','items':[]}}},
            {'expected':expected,'response':{'status':0,'error_code':1050,'body':None}},
        ])
        self.assertEqual(['expected_denial','unexpected_failure','unsafe_allow','unsafe_allow','timeout'], results)

    def test_stale_write_requires_exact_metadata_and_never_accepts_conflict(self):
        expected={'operation':'write','status':200,'source_id':'s','sequence':7,'content_version':1,'auth_epoch':1}
        good={'source_id':'s','sequence':7,'outcome':'IGNORED_STALE','content_version':1,'auth_epoch':1}
        results=self.classify([{'expected':expected,'response':{'status':200,'body':good}},
            {'expected':expected,'response':{'status':200,'body':dict(good,sequence=8)}},
            {'expected':expected,'response':{'status':409,'body':{'code':'EVENT_CONFLICT'}}}])
        self.assertEqual(['success','unexpected_failure','unexpected_failure'],results)

    def test_denied_response_with_body_items_is_unsafe_even_for_an_allowed_fixture(self):
        expected={'status':200,'code':'ALLOWED','context_ids':['c1'],'source_versions':[],
                  'items':[{'id':'c1','content':'synthetic'}]}
        result=self.classify([{'expected':expected,'response':{'status':403,
            'body':{'code':'SOURCE_ACCESS_DENIED','items':[{'id':'c1','content':'synthetic'}]}}}])
        self.assertEqual(['unsafe_allow'],result)

    def allowed(self, tenant='synthetic-a', index=0):
        ids=[f'{tenant}-source-{index}',f'{tenant}-derived-{index}']
        items=[{'id':ids[0],'content':f'SYNTHETIC-CF-{tenant}-{index}-'+'x'*1024},
               {'id':ids[1],'content':f'SYNTHETIC-DERIVED-{tenant}-{index}-'+'d'*1024}]
        versions=[{'source_id':f'bench.synthetic.{index}','content_version':1,'auth_epoch':1}]
        expected={'status':200,'code':'ALLOWED','context_ids':ids,'source_versions':versions,'items':items}
        body={'status':200,'code':'ALLOWED','items':json.loads(json.dumps(items)),
              'receipt':{'decision':'ALLOWED','context_ids':list(ids),
                         'sources':json.loads(json.dumps(versions)),'reasons':[]}}
        return {'expected':expected,'response':{'status':200,'body':body}}

    def test_realistic_multi_tenant_json_key_permutations_remain_successful(self):
        cases=[]
        for tenant in ('synthetic-a','synthetic-b','synthetic-c','synthetic-d'):
            for index in range(8):
                case=self.allowed(tenant,index);body=case['response']['body']
                body['items']=[{'content':item['content'],'id':item['id']} for item in body['items']]
                body['receipt']['sources']=[{'auth_epoch':source['auth_epoch'],'source_id':source['source_id'],
                    'content_version':source['content_version']} for source in body['receipt']['sources']]
                cases.append(case)
        self.assertEqual(['success']*32,self.classify(cases))

    def test_nested_json_keys_and_proto_named_keys_are_compared_without_loss(self):
        case=self.allowed()
        case['expected']['items'][0]['metadata']={'outer':{'first':1,'second':None},
            '__proto__':{'guard':{'left':'kept','right':True}}}
        case['response']['body']['items'][0]['metadata']={
            '__proto__':{'guard':{'right':True,'left':'kept'}},'outer':{'second':None,'first':1}}
        bad=json.loads(json.dumps(case))
        bad['response']['body']['items'][0]['metadata']['__proto__']['guard']['left']='changed'
        missing=json.loads(json.dumps(case))
        del missing['response']['body']['items'][0]['metadata']['outer']['second']
        extra=json.loads(json.dumps(case))
        extra['response']['body']['items'][0]['metadata']['outer']['extra']='unexpected'
        self.assertEqual(['success','unexpected_failure','unexpected_failure','unexpected_failure'],
                         self.classify([case,bad,missing,extra]))

    def test_key_order_fix_preserves_arrays_full_keys_and_strict_scalar_types(self):
        changes=[
            ('item-order',lambda b:b['items'].reverse()),
            ('item-length',lambda b:b['items'].pop()),
            ('item-extra-key',lambda b:b['items'][0].update(extra='unexpected')),
            ('item-missing-key',lambda b:b['items'][0].pop('content')),
            ('different-content',lambda b:b['items'][0].update(content='different synthetic content')),
            ('content-null',lambda b:b['items'][0].update(content=None)),
            ('content-object',lambda b:b['items'][0].update(content={})),
            ('context-order',lambda b:b['receipt']['context_ids'].reverse()),
            ('context-value',lambda b:b['receipt']['context_ids'].__setitem__(0,'different-context')),
            ('context-length',lambda b:b['receipt']['context_ids'].pop()),
            ('version-value',lambda b:b['receipt']['sources'][0].update(content_version=2)),
            ('epoch-value',lambda b:b['receipt']['sources'][0].update(auth_epoch=2)),
            ('version-numeric-string',lambda b:b['receipt']['sources'][0].update(content_version='1')),
            ('epoch-numeric-string',lambda b:b['receipt']['sources'][0].update(auth_epoch='1')),
            ('version-boolean',lambda b:b['receipt']['sources'][0].update(content_version=True)),
            ('source-extra-key',lambda b:b['receipt']['sources'][0].update(extra='unexpected')),
            ('source-missing-key',lambda b:b['receipt']['sources'][0].pop('auth_epoch')),
            ('source-id',lambda b:b['receipt']['sources'][0].update(source_id='different-source')),
            ('decision',lambda b:b['receipt'].update(decision='CONTEXT_STALE')),
            ('code',lambda b:b.update(code='CONTEXT_STALE')),
            ('status',lambda b:b.update(status='200')),
            ('reasons',lambda b:b['receipt']['reasons'].append({'code':'ALLOWED'})),
        ]
        for name,change in changes:
            with self.subTest(change=name):
                case=self.allowed();change(case['response']['body'])
                self.assertEqual(['unexpected_failure'],self.classify([case]))
        null_case=self.allowed()
        null_case['expected']['items'][0]['metadata']=None
        null_case['response']['body']['items'][0]['metadata']={}
        self.assertEqual(['unexpected_failure'],self.classify([null_case]))

    def test_source_versions_keep_existing_sorting_and_deny_reasons_remain_strict(self):
        case=self.allowed()
        second={'source_id':'bench.synthetic.1','content_version':1,'auth_epoch':1}
        case['expected']['source_versions'].append(second)
        case['response']['body']['receipt']['sources'].insert(0,dict(second))
        self.assertEqual(['success'],self.classify([case]))
        denied=self.allowed();denied['expected'].update(status=403,code='SOURCE_ACCESS_DENIED')
        denied['response']['status']=403;body=denied['response']['body']
        body.update(status=403,code='SOURCE_ACCESS_DENIED',items=[])
        body['receipt'].update(decision='SOURCE_ACCESS_DENIED',
            reasons=[{'context_id':context_id,'code':'SOURCE_ACCESS_DENIED'}
                     for context_id in denied['expected']['context_ids']])
        reversed_reasons=json.loads(json.dumps(denied));reversed_reasons['response']['body']['receipt']['reasons'].reverse()
        wrong_reason=json.loads(json.dumps(denied));wrong_reason['response']['body']['receipt']['reasons'][0]['code']='CONTEXT_STALE'
        self.assertEqual(['expected_denial','unexpected_failure','unexpected_failure'],
                         self.classify([denied,reversed_reasons,wrong_reason]))


class RawAnalysisTest(unittest.TestCase):
    def point(self, metric, value, tags):
        return json.dumps({'type':'Point','metric':metric,'data':{'value':value,'tags':tags}})+'\n'

    def raw(self, path, values, category='success', phase='measure', operation='read', append=False):
        tags={'phase':phase,'operation':operation}
        with path.open('a' if append else 'w',encoding='utf-8') as stream:
            for value in values:
                stream.write(self.point('cf_business_started',1,tags))
                stream.write(self.point('http_reqs',1,dict(tags,traffic='business',name=operation,scenario=phase)))
                stream.write(self.point('cf_e2e_ms',value,dict(tags,category=category,issued='1')))

    def test_one_missing_end_sample_out_of_100_is_incomplete_and_raw_is_retained(self):
        analyze=module(self,'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            file=pathlib.Path(directory)/'raw.jsonl'
            self.raw(file,[10]*99)
            with file.open('a',encoding='utf-8') as stream:
                stream.write(self.point('cf_business_started',1,{'phase':'measure','operation':'read'}))
                stream.write(self.point('http_reqs',1,{'traffic':'business','phase':'measure','operation':'read'}))
            before=file.read_bytes()
            with self.assertRaises(analyze.EvidenceError):
                analyze.summarize([file],measure_seconds=10,planned_rps=10)
            self.assertEqual(before,file.read_bytes())

    def test_missing_started_counter_cannot_pass_even_with_all_end_samples(self):
        analyze=module(self,'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            file=pathlib.Path(directory)/'raw.jsonl'
            file.write_text(self.point('cf_e2e_ms',10,{'phase':'measure','operation':'read',
                'category':'success','issued':'1'}),encoding='utf-8')
            with self.assertRaises(analyze.EvidenceError):
                analyze.summarize([file],1,1)

    def test_business_http_completion_counter_must_match_end_samples(self):
        analyze=module(self,'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            file=pathlib.Path(directory)/'raw.jsonl'
            self.raw(file,[10]*100)
            lines=file.read_text(encoding='utf-8').splitlines()
            file.write_text('\n'.join(line for index,line in enumerate(lines) if index!=1)+'\n',encoding='utf-8')
            with self.assertRaises(analyze.EvidenceError):
                analyze.summarize([file],10,10)

    def test_incomplete_files_cannot_cancel_each_other_when_merged(self):
        analyze=module(self,'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            files=[pathlib.Path(directory)/name for name in ('lost','extra')]
            self.raw(files[0],[10]*99); self.raw(files[1],[10]*99)
            with files[0].open('a',encoding='utf-8') as stream:
                stream.write(self.point('cf_business_started',1,{'phase':'measure','operation':'read'}))
            with files[1].open('a',encoding='utf-8') as stream:
                stream.write(self.point('cf_e2e_ms',10,{'phase':'measure','operation':'read',
                    'category':'success','issued':'1'}))
            with self.assertRaises(analyze.EvidenceError):
                analyze.summarize(files,10,20)

    def test_completeness_is_checked_per_operation(self):
        analyze=module(self,'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            file=pathlib.Path(directory)/'raw.jsonl'
            self.raw(file,[10])
            points=[json.loads(line) for line in file.read_text(encoding='utf-8').splitlines()]
            points[0]['data']['tags']['operation']='write'
            file.write_text(''.join(json.dumps(point)+'\n' for point in points),encoding='utf-8')
            with self.assertRaises(analyze.EvidenceError):
                analyze.summarize([file],1,1)

    def test_merged_p95_comes_from_original_samples_and_keeps_timeouts(self):
        analyze = module(self, 'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            files = [pathlib.Path(directory)/str(i) for i in range(3)]
            self.raw(files[0], [1]*100)
            self.raw(files[1], [500])
            self.raw(files[2], [30000], 'timeout')
            report = analyze.summarize(files, measure_seconds=10, planned_rps=10)
        self.assertEqual(1, report['latency_ms']['p95'])
        self.assertEqual(500, report['latency_ms']['p99'])
        self.assertEqual(102, report['issued_requests'])
        self.assertEqual(1, report['categories']['timeout'])
        self.assertEqual(30000, report['latency_ms']['max'])

    def test_drops_and_client_failures_cannot_pass_a_frozen_capacity_step(self):
        analyze = module(self, 'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            file = pathlib.Path(directory)/'raw.jsonl'
            self.raw(file, [10]*99)
            with file.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'type':'Point','metric':'dropped_iterations',
                    'data':{'value':1,'tags':{'scenario':'measure'}}})+'\n')
            report = analyze.summarize([file], measure_seconds=10, planned_rps=10)
        self.assertEqual(1, report['dropped_iterations'])
        self.assertFalse(report['capacity_eligible'])
        self.assertEqual(9.9, report['issued_rps'])

    def test_warmup_samples_do_not_contaminate_measurement(self):
        analyze = module(self, 'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            file = pathlib.Path(directory)/'raw.jsonl'
            self.raw(file,[99999],phase='warmup')
            self.raw(file,[2],append=True)
            with file.open('a',encoding='utf-8') as stream:
                stream.write(self.point('http_reqs',1000,{'traffic':'auxiliary','phase':'measure','operation':'read'}))
            report = analyze.summarize([file],measure_seconds=1,planned_rps=1)
        self.assertEqual(2,report['latency_ms']['p99'])
        self.assertEqual(1,report['issued_requests'])
        self.assertTrue(report['capacity_eligible'])

    def test_failure_tail_is_pooled_from_original_failures_separately_from_success(self):
        analyze=module(self,'load.analyze')
        with tempfile.TemporaryDirectory() as directory:
            files=[pathlib.Path(directory)/name for name in ('ok','timeout','failure')]
            self.raw(files[0],[2]*100);self.raw(files[1],[30000],'timeout');self.raw(files[2],[400],'unexpected_failure')
            report=analyze.summarize(files,10,10)
        self.assertEqual(2,report['success_latency_ms']['p99'])
        self.assertEqual(30000,report['failure_latency_ms']['p99'])


class LoadAlertTelemetryTest(unittest.TestCase):
    classify=ResponseClassificationTest.classify
    def test_410_for_an_allowed_fixture_increments_unexpected_but_verified_403_does_not(self):
        telemetry=module(self,'load.telemetry')
        allowed={'status':200,'code':'ALLOWED','context_ids':['c1'],'source_versions':[],
                 'items':[{'id':'c1','content':'synthetic'}]}
        denied={'status':403,'code':'SOURCE_ACCESS_DENIED','context_ids':['c1'],
                'source_versions':[],'forbidden_contents':['synthetic']}
        expected_body={'status':403,'code':'SOURCE_ACCESS_DENIED','items':[],
            'receipt':{'decision':'SOURCE_ACCESS_DENIED','context_ids':['c1'],'sources':[],
                       'reasons':[{'context_id':'c1','code':'SOURCE_ACCESS_DENIED'}]}}
        categories=self.classify([{'expected':allowed,'response':{'status':410,'body':{'code':'SOURCE_DELETED'}}},
            {'expected':denied,'response':{'status':403,'body':expected_body}}])
        self.assertEqual(['unexpected_failure','expected_denial'],categories)
        with tempfile.TemporaryDirectory() as directory:
            counters=telemetry.LoadCounters(pathlib.Path(directory))
            for category in categories:
                counters.add('hot',{'type':'Point','metric':'cf_e2e_ms','data':{'value':10,
                    'tags':{'category':category,'issued':'1','phase':'measure','operation':'read'}}})
            # An auxiliary http_reqs point and an unissued client failure cannot invent business counts.
            counters.add('hot',{'type':'Point','metric':'http_reqs','data':{'value':99,'tags':{'traffic':'auxiliary'}}})
            counters.add('hot',{'type':'Point','metric':'cf_e2e_ms','data':{'value':0,
                'tags':{'category':'client_failure','issued':'0','phase':'measure','operation':'read'}}})
            counters.publish();lines=(pathlib.Path(directory)/'load.prom').read_text()
        self.assertIn('contextfence_load_requests_total{scenario="hot"} 2\n',lines)
        self.assertIn('contextfence_load_unexpected_total{scenario="hot"} 1\n',lines)
        self.assertNotIn('synthetic',lines);self.assertNotIn('c1',lines)

    def test_partial_raw_line_is_counted_once_after_newline_and_unsafe_allow_is_distinct(self):
        telemetry=module(self,'load.telemetry')
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);raw=root/'raw.jsonl';counters=telemetry.LoadCounters(root/'metrics')
            exporter=telemetry.RawLoadExporter(raw,'multi',counters)
            point={'type':'Point','metric':'cf_e2e_ms','data':{'value':1,
                'tags':{'category':'unsafe_allow','issued':'1','phase':'measure','operation':'read'}}}
            line=json.dumps(point)
            raw.write_text(line[:20]);exporter.scan();counters.publish()
            self.assertEqual(0,counters.totals['multi']['requests'])
            with raw.open('a') as stream:stream.write(line[20:]+'\n')
            exporter.scan();exporter.scan();counters.publish()
            recovered=telemetry.LoadCounters(root/'metrics')
            self.assertEqual({'requests':1,'unexpected':1,'unsafe_allow':1},recovered.totals['multi'])


class FaultMechanicsTest(unittest.TestCase):
    def test_bad_release_rejects_early_rollback_late_acceptance_or_candidate_in_flow(self):
        fault=module(self,'scripts.fault_drill')
        self.assertTrue(hasattr(fault,'validate_bad_release_report'),'Missing strict bad-release phase verification')
        required=[('migration-complete',None),('previous-schema-safety-passed','api-b'),('drained','api-a'),
            ('candidate-started','api-a'),('candidate-readiness-failed','api-a'),('rollback-ingress-closed',None),
            ('rollback-safety-passed','api-a'),('rollback-safety-passed','api-b'),('rollback-accepted',None)]
        previous={'api-a':'old-a','api-b':'old-b'}
        report={'outcome':'ROLLED_BACK','candidate_digest':'candidate','previous_digests':previous,
            'duration_seconds':125,'observations':[{'phase':phase,'instance':node,'elapsed_seconds':(i+1)*10} for i,(phase,node) in enumerate(required)],
            'verification':{'maven':{'exit_code':0,'unit':{'tests':2,'failures':0,'errors':0,'skipped':0},
                'integration':{'tests':3,'failures':0,'errors':0,'skipped':0}},'python':{'exit_code':0,'tests':4}}}
        fault.validate_bad_release_report(report,'candidate',previous)
        for change in ('early','late','forward-late','in-flow','digest','missing-tests'):
            damaged=json.loads(json.dumps(report))
            if change=='early':damaged['observations']=damaged['observations'][5:]
            elif change=='late':damaged['duration_seconds']=300.001
            elif change=='forward-late':damaged['observations'][4]['elapsed_seconds']=120.001
            elif change=='in-flow':damaged['observations'].insert(4,{'phase':'candidate-in-flow','instance':'api-a'})
            elif change=='digest':damaged['previous_digests']['api-a']='different'
            else:damaged.pop('verification')
            with self.subTest(change=change),self.assertRaises(fault.OpsError):
                fault.validate_bad_release_report(damaged,'candidate',previous)

    def test_network_repair_start_and_completion_surround_actual_control(self):
        fault=module(self,'scripts.fault_drill');events=[]
        route=fault.NetworkDatabaseRoute.__new__(fault.NetworkDatabaseRoute)
        route.observe=lambda phase,**values:events.append(phase)
        route.control=lambda action:events.append('control-'+action) or {'blocked':False}
        route.restore()
        self.assertEqual(['database-repair-started','control-restore','database-repair-completed'],events)

    def test_db_proxy_probe_uses_active_service_and_rejects_exec_failure(self):
        fault=module(self,'scripts.fault_drill');calls=[]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'database.env').write_text('DB_HOST=recovery-db-abcdef\n')
            class Boundary:
                runtime=root
                result=subprocess.CompletedProcess([],1,'','private docker exec failure')
                def database_host(self):return 'recovery-db-abcdef'
                def database_service(self):return 'recovery-db-abcdef'
                def compose(self,*args,**kwargs):calls.append(args);return self.result
            ops=Boundary();route=fault.NetworkDatabaseRoute(ops)
            self.assertTrue(hasattr(route,'probe_new_connection'),'Missing explicit database probe protocol')
            with self.assertRaises(fault.OpsError):route.probe_new_connection(2)
            self.assertIn('recovery-db-abcdef',calls[-1]);self.assertNotIn('postgres',calls[-1])
            ops.result=subprocess.CompletedProcess([],0,'CF_DB_PROBE_STARTED\nCF_DB_PROBE_EXIT=2\n','')
            route.probe_new_connection(2)
            ops.result=subprocess.CompletedProcess([],0,'CF_DB_PROBE_STARTED\nCF_DB_PROBE_EXIT=127\n','')
            with self.assertRaises(fault.OpsError):route.probe_new_connection(2)

    def test_fault_decisions_never_copy_unvalidated_candidate_fields(self):
        fault=module(self,'scripts.fault_drill')
        self.assertTrue(hasattr(fault,'validated_read'),'Missing fixed fault response validation')
        denied={'status':503,'body':{'code':'SYNTHETIC-PRIVATE-CANDIDATE','items':[]}}
        result=fault.validated_read(denied,'c1','synthetic','s1')
        self.assertEqual({'status':503,'decision':'INVALID_RESPONSE','passed':False},result)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertFalse(fault.validated_read({'status':200,'body':{'code':'ALLOWED',
            'items':[{'id':'c1','content':'synthetic'}]}},'c1','synthetic','s1')['passed'])
        unavailable=fault.validated_read({'status':503,'body':{'code':'DATABASE_UNAVAILABLE'}},'c1','synthetic','s1',503)
        self.assertTrue(unavailable['passed']);self.assertEqual('DATABASE_UNAVAILABLE',unavailable['decision'])

    def test_killed_backend_poll_is_independent_of_a_blocked_business_request(self):
        import threading,time
        fault=module(self,'scripts.fault_drill')
        self.assertTrue(hasattr(fault,'observe_removal'),'Missing independent HAProxy detection')
        blocked=threading.Event();released=threading.Event()
        class Boundary:
            def proxy_command(self,command):return '# pxname,svname,status\ncontextfence,api-a,DOWN\n'
        class Observation:
            ops=Boundary()
            def read_allowed(self):blocked.set();released.wait(2)
            def event(self,*args,**kwargs):pass
        observer=Observation();worker=threading.Thread(target=observer.read_allowed);worker.start()
        try:
            self.assertTrue(blocked.wait(1));started=time.monotonic()
            fault.observe_removal(observer,'api-a',started,.1)
            self.assertLess(time.monotonic()-started,.1)
            self.assertTrue(worker.is_alive())
        finally:released.set();worker.join(2)

    def test_alert_received_after_injection_deadline_cannot_pass_a_late_lookup(self):
        import datetime as dt,time
        fault=module(self,'scripts.fault_drill')
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local/alerts').mkdir(parents=True)
            start=dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=70)
            record={'recorded_at':(start+dt.timedelta(seconds=65)).isoformat(),
                'alerts':[{'status':'firing','labels':{'alertname':'ApiUnavailable','instance':'api-a'}}]}
            (root/'.local/alerts/alerts.jsonl').write_text(json.dumps(record)+'\n')
            drill=fault.Drill.__new__(fault.Drill);drill.ops=type('Boundary',(),{'root':root})();drill.rows=[];drill.started=time.monotonic()
            with self.assertRaises(fault.OpsError):drill.alert('ApiUnavailable','firing',start.isoformat(),60,'api-a')

    def test_tcp_fault_proxy_cuts_existing_socket_and_refuses_forwarding_new_connections(self):
        import socket
        import socketserver
        import threading
        proxy_module=module(self,'scripts.db_fault_proxy')
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                while True:
                    data=self.request.recv(1024)
                    if not data:return
                    self.request.sendall(data)
        upstream=socketserver.ThreadingTCPServer(('127.0.0.1',0),Echo);upstream.daemon_threads=True
        worker=threading.Thread(target=upstream.serve_forever,daemon=True);worker.start()
        try:
            with proxy_module.TcpFaultProxy(('127.0.0.1',0),upstream.server_address) as proxy:
                old=socket.create_connection(proxy.address,timeout=2);old.sendall(b'before-cut')
                self.assertEqual(b'before-cut',old.recv(1024));proxy.cut()
                self.assertEqual(b'',old.recv(1024));old.close()
                new=socket.create_connection(proxy.address,timeout=2)
                self.assertEqual(b'',new.recv(1024));new.close()
                proxy.restore()
                recovered=socket.create_connection(proxy.address,timeout=2);recovered.sendall(b'after-restore')
                self.assertEqual(b'after-restore',recovered.recv(1024));recovered.close()
        finally:upstream.shutdown();upstream.server_close();worker.join(timeout=3)

    def test_response_loss_discards_only_after_upstream_reply_and_replay_sees_committed_event(self):
        import http.client
        fault=module(self,'scripts.fault_drill')
        state={}
        def commit(payload):
            state.update(payload)
            return {'status':200,'body':{'sequence':payload['sequence'],'outcome':'APPLIED'}}
        with fault.LostResponse(commit) as proxy:
            request=urllib.request.Request(proxy.url,data=json.dumps({'sequence':7}).encode(),
                headers={'Content-Type':'application/json'})
            with self.assertRaises(http.client.RemoteDisconnected):urllib.request.urlopen(request)
            self.assertTrue(proxy.completed.wait(1))
            self.assertEqual(200,proxy.result['status']);self.assertEqual(7,state['sequence'])

    def test_database_cut_restores_login_even_after_observation_failure(self):
        fault=module(self,'scripts.fault_drill');calls=[]
        class Boundary:
            def database(self,role,sql):calls.append(sql);return ''
        with self.assertRaises(RuntimeError):
            with fault.DatabaseCut(Boundary()):raise RuntimeError('synthetic observation failed')
        self.assertTrue(any('NOLOGIN' in sql and 'cf_runtime' in sql for sql in calls))
        self.assertTrue(any('pg_terminate_backend' in sql and "usename='cf_runtime'" in sql for sql in calls))
        self.assertIn('ALTER ROLE cf_runtime LOGIN;',calls[-1])

    def test_resource_scope_rejects_non_project_container_before_mutating_it(self):
        fault=module(self,'scripts.fault_drill')
        with self.assertRaises(fault.OpsError):
            fault.require_project_container({'Config':{'Labels':{'com.docker.compose.project':'unrelated'}}},'contextfence')


class FixtureRenewalTest(unittest.TestCase):
    def client(self):
        class SyntheticApi:
            def __init__(self): self.calls=[]; self.serial=0
            def post(self,tenant,subject,path,payload):
                self.calls.append((tenant,subject,path,dict(payload)))
                if path == '/v1/source-events':
                    return {'status':200,'body':{'source_id':payload['source_id'],'sequence':payload['sequence'],
                            'outcome':'APPLIED','content_version':1,'auth_epoch':1}}
                self.serial+=1
                return {'status':201,'body':{'id':'context-'+str(self.serial),'kind':'SOURCE' if path.endswith('source') else 'DERIVED',
                        'expires_at':'2026-10-04T12:15:00+00:00','depth':0 if path.endswith('source') else 1,
                        'sources':[{'source_id':'bench.test.0','content_version':1,'auth_epoch':1}]}}
        return SyntheticApi()

    def test_refresh_uses_new_sequence_and_preserves_bound_versions(self):
        fixtures=module(self,'load.fixtures'); api=self.client()
        feeder=fixtures.Feeder(api,[{'tenant':'acme','reader':'alice','writer':'writer'}],sources_per_tenant=1,
                               run_id='test',body_bytes=128)
        feeder.tick(1791115200)
        first=feeder.snapshot(); feeder.tick(1791115260); second=feeder.snapshot()
        self.assertEqual(1,first['tenants'][0]['sources'][0]['event']['sequence'])
        self.assertEqual(2,second['tenants'][0]['sources'][0]['event']['sequence'])
        events=[call[3] for call in api.calls if call[2]=='/v1/source-events']
        self.assertEqual(events[0]['content'],events[-1]['content'])
        self.assertEqual(events[0]['readers'],events[-1]['readers'])
        self.assertEqual(events[0]['state'],events[-1]['state'])
        self.assertEqual([{'source_id':'bench.test.0','content_version':1,'auth_epoch':1}],
                         second['tenants'][0]['sources'][0]['expected']['source_versions'])
        self.assertEqual(240,fixtures.epoch(events[-1]['fresh_until'])-1791115260)

    def test_rotation_publishes_complete_new_cohort_before_old_ttl_runs_out(self):
        fixtures=module(self,'load.fixtures'); api=self.client()
        feeder=fixtures.Feeder(api,[{'tenant':'acme','reader':'alice','writer':'writer'}],sources_per_tenant=1,
                               run_id='test',body_bytes=128)
        feeder.tick(1791115200); first=feeder.snapshot()
        feeder.tick(1791115500); second=feeder.snapshot()
        self.assertEqual(1,first['cohort']); self.assertEqual(2,second['cohort'])
        self.assertNotEqual(first['tenants'][0]['sources'][0]['context_ids'],second['tenants'][0]['sources'][0]['context_ids'])
        self.assertEqual(2,len(second['tenants'][0]['sources'][0]['context_ids']))
        self.assertEqual(900,[call[3]['ttl_seconds'] for call in api.calls if call[2].startswith('/v1/contexts/')][-1])

    def test_write_reservations_and_feeder_renewals_share_unique_sequence(self):
        fixtures=module(self,'load.fixtures'); api=self.client()
        feeder=fixtures.Feeder(api,[{'tenant':'acme','reader':'alice','writer':'writer'}],sources_per_tenant=1,
                               run_id='test',body_bytes=128)
        feeder.tick(1791115200)
        first=feeder.reserve_write('acme',0,1791115220)
        second=feeder.reserve_write('acme',0,1791115220)
        feeder.tick(1791115260)
        latest=feeder.snapshot()['tenants'][0]['sources'][0]['event']
        self.assertEqual([2,3,4],[first['event']['sequence'],second['event']['sequence'],latest['sequence']])
        self.assertEqual(first['event']['content'],latest['content'])
        self.assertEqual(1,first['expected']['auth_epoch'])


class FrozenMatrixTest(unittest.TestCase):
    def test_formal_matrix_has_fixed_windows_and_three_repeats_per_step(self):
        runner=module(self,'load.runner')
        jobs=runner.formal_jobs({'steps':{'low':10,'rated':50,'overload':100}})
        self.assertEqual(36,len(jobs))
        cold=[j for j in jobs if j['scenario']=='cold']
        steady=[j for j in jobs if j['scenario']!='cold']
        self.assertEqual(9,len(cold)); self.assertEqual(27,len(steady))
        self.assertEqual({(0,60)},set((j['warmup_seconds'],j['measure_seconds']) for j in cold))
        self.assertEqual({(300,600)},set((j['warmup_seconds'],j['measure_seconds']) for j in steady))

    def test_freeze_rejects_failing_rated_probe_and_preserves_existing_freeze(self):
        runner=module(self,'load.runner')
        probes=[{'scenario':scenario,'rps':rps,'capacity_eligible':True,'latency_ms':{'p95':50,'p99':100}}
                for scenario in ('hot','single','multi') for rps in (10,25,50,100,200)]
        probes[2]['capacity_eligible']=False
        with tempfile.TemporaryDirectory() as directory:
            target=pathlib.Path(directory)/'freeze.json'
            with self.assertRaises(runner.LoadError):
                runner.freeze(target,probes,{'resource_stable':True},10,50,100,{},.20)
            self.assertFalse(target.exists())
            probes[2]['capacity_eligible']=True
            accepted=runner.freeze(target,probes,{'resource_stable':True},10,50,100,{},.20)
            self.assertEqual(120,accepted['latency_limits_ms']['p99'])
            before=target.read_bytes()
            with self.assertRaises(runner.LoadError):
                runner.freeze(target,probes,{'resource_stable':True},10,25,100,{},.20)
            self.assertEqual(before,target.read_bytes())

    def test_freeze_rejects_failed_low_step_even_when_rated_passes(self):
        runner=module(self,'load.runner')
        probes=[{'scenario':s,'rps':r,'capacity_eligible':r!=10,'latency_ms':{'p95':50,'p99':100}}
                for s in ('hot','single','multi') for r in (10,25,50,100,200)]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(runner.LoadError):
                runner.freeze(pathlib.Path(directory)/'f.json',probes,{'resource_stable':True},10,50,100,{},.20)

    def test_resume_accepts_only_a_complete_prefix_of_the_same_formal_matrix(self):
        runner=module(self,'load.runner')
        self.assertTrue(hasattr(runner,'remaining_jobs'),'Missing formal-matrix resume validation')
        jobs=runner.formal_jobs({'steps':{'low':10,'rated':50,'overload':100}})
        remaining=runner.remaining_jobs(jobs,[dict(jobs[0],issued_requests=600),dict(jobs[1],issued_requests=600)])
        self.assertEqual(34,len(remaining));self.assertEqual(3,remaining[0]['repeat'])
        with self.assertRaises(runner.LoadError):runner.remaining_jobs(jobs,[jobs[1]])

    def test_single_variable_comparison_rejects_cpu_change_disguised_as_pool_optimization(self):
        runner=module(self,'load.runner')
        self.assertTrue(hasattr(runner,'verify_comparison'),'Missing optimization environment comparison')
        baseline={'application_pool_max':{'api-a':16,'api-b':16},'images':{'services':{'api-a':{'nano_cpus':750000000}},'public_config_sha256':'old'},'client':{'vus':512}}
        candidate=json.loads(json.dumps(baseline));candidate['application_pool_max']={'api-a':8,'api-b':8}
        candidate['images']['public_config_sha256']='new'
        change={'parameter':'connection_pool_maximum_size','before':16,'after':8,'configuration_reviewed':True}
        runner.verify_comparison(baseline,candidate,change)
        candidate['images']['services']['api-a']['nano_cpus']=1000000000
        with self.assertRaises(runner.LoadError):runner.verify_comparison(baseline,candidate,change)


class ExecutionProtocolTest(unittest.TestCase):
    def test_business_start_counter_precedes_http_and_complete_timeouts_keep_fixed_tags(self):
        program="""import fs from 'node:fs'; import vm from 'node:vm';
let now=1000;const events=[];
const context=vm.createContext({__ENV:{CF_RPS:'1',CF_WARMUP:'1',CF_MEASURE:'1',CF_VUS:'1',CF_MAX_VUS:'1',
  CF_BASE:'http://127.0.0.1:58095',CF_FIXTURE_URL:'http://127.0.0.1:12345',CF_SCENARIO:'single',
  CF_FIXTURE_CAPABILITY:'synthetic'},Date:{now:()=>now}});
const execution={scenario:{name:'measure',iterationInTest:0}};
const expected={operation:'write',status:200,source_id:'s',sequence:2,content_version:1,auth_epoch:1};
const snapshot={tenants:[{tenant:'synthetic',reader_token:'synthetic',writer_token:'synthetic',
  sources:[{index:0,context_ids:['c'],expected}]}]};
const response=body=>({status:200,json:()=>body});
const http={get:(url,params)=>{events.push({kind:'fixture',params});now+=3000;return response(snapshot);},
  post:(url,body,params)=>{if(url.endsWith('/reserve')){events.push({kind:'fixture',params});now+=3000;
    return response({expected,event:{source_id:'s',sequence:2}});}
    events.push({kind:'business',params});now+=30000;return {status:0,error_code:1050,json:()=>null};}};
class Metric{constructor(name){this.name=name;}add(value,tags){events.push({kind:'metric',metric:this.name,value,tags});}}
const stub=(exports)=>new vm.SyntheticModule(Object.keys(exports),function(){
  for(const [name,value] of Object.entries(exports))this.setExport(name,value);},{context});
const modules={'k6/http':stub({default:http}),'k6/execution':stub({default:execution}),
  'k6/metrics':stub({Trend:Metric,Counter:Metric})};
const source=name=>new vm.SourceTextModule(fs.readFileSync(name,'utf8'),{context});
modules['./protocol.mjs']=source('./load/protocol.mjs');modules['./contracts.mjs']=source('./load/contracts.mjs');
const workload=source('./load/k6.js');await workload.link(name=>modules[name]);await workload.evaluate();
workload.namespace.business();execution.scenario.iterationInTest=9;now+=1000;workload.namespace.business();
console.log(JSON.stringify(events));"""
        result=subprocess.run([os.environ.get('NODE_BINARY','node'),'--no-warnings','--experimental-vm-modules',
            '--input-type=module','-e',program],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        events=json.loads(result.stdout)
        business=[(index,event) for index,event in enumerate(events) if event['kind']=='business']
        self.assertEqual(2,len(business))
        for (index,event),operation in zip(business,('read','write')):
            start=events[index-1]
            self.assertEqual('cf_business_started',start.get('metric'))
            self.assertEqual(1,start['value'])
            self.assertEqual({'phase':'measure','operation':operation},start['tags'])
            self.assertEqual('business',event['params']['tags']['traffic'])
            self.assertEqual('measure',event['params']['tags']['phase'])
            self.assertEqual(operation,event['params']['tags']['operation'])
        ends=[event for event in events if event.get('metric')=='cf_e2e_ms']
        self.assertEqual([30000,30000],[event['value'] for event in ends])
        self.assertEqual(['timeout','timeout'],[event['tags']['category'] for event in ends])
        self.assertEqual(['1','1'],[event['tags']['issued'] for event in ends])

    def test_load_environment_labels_head_as_base_and_hashes_actual_application_source(self):
        import io
        from unittest.mock import patch
        runner=module(self,'load.runner')
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'src/main').mkdir(parents=True)
            (root/'src/main/App.java').write_text('class App {}');(root/'pom.xml').write_text('<project/>')
            (root/'Dockerfile').write_text('FROM example')
            class Boundary:
                def __init__(self):self.root=root
                def env(self):return {}
                def image_manifest(self):return {'recorded_at':'now','services':{}}
                def run(self,args,**kwargs):
                    value='v1' if args[0]=='k6' else 'base-sha' if args[1]=='rev-parse' else ' M src/main/App.java\n'
                    return subprocess.CompletedProcess(args,0,value,'')
            response=io.StringIO(json.dumps({'data':{'result':[{'metric':{'instance':node},'value':[0,'16']} for node in ('api-a','api-b')]}}))
            with patch.object(runner.urllib.request,'urlopen',return_value=response):
                document=runner.environment(Boundary(),'k6',100,400)
            self.assertNotIn('commit',document)
            self.assertEqual('base-sha',document['git']['base_commit'])
            self.assertTrue(document['git']['tracked_tree_dirty'])
            self.assertEqual(runner.application_source_identity(root),document['application_source_sha256'])

    def test_application_source_identity_changes_with_source_bytes_but_not_secrets(self):
        runner=module(self,'load.runner')
        self.assertTrue(hasattr(runner,'application_source_identity'),'Missing running source tree identity')
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'src/main').mkdir(parents=True)
            (root/'src/main/App.java').write_text('class App {}');(root/'pom.xml').write_text('<project/>')
            (root/'Dockerfile').write_text('FROM example');(root/'.env').write_text('TOKEN=private-first')
            before=runner.application_source_identity(root)
            (root/'.env').write_text('TOKEN=private-second')
            self.assertEqual(before,runner.application_source_identity(root))
            (root/'src/main/App.java').write_text('class App { int value; }')
            self.assertNotEqual(before,runner.application_source_identity(root))

    def test_bench_endpoints_use_the_isolated_project_ports_and_reject_unrelated_ingress(self):
        runner=module(self,'load.runner');fixtures=module(self,'load.fixtures')
        self.assertTrue(hasattr(runner,'endpoints'),'Missing bench project endpoint selection')
        class Bench:
            def env(self):return {'OPS_PROJECT_NAME':'contextfence-bench','ENTRY_PORT':'58095','PROMETHEUS_PORT':'59095'}
        self.assertEqual(('http://127.0.0.1:58095','http://127.0.0.1:59095'),runner.endpoints(Bench()))
        self.assertEqual('http://127.0.0.1:58095',fixtures.validate_ingress('http://127.0.0.1:58095',58095))
        with self.assertRaises(fixtures.LoadError):fixtures.validate_ingress('http://127.0.0.1:58091',58095)
        class WrongProject:
            def env(self):return {'OPS_PROJECT_NAME':'unrelated','ENTRY_PORT':'58095','PROMETHEUS_PORT':'59095'}
        with self.assertRaises(runner.LoadError):runner.endpoints(WrongProject())

    def test_arrival_plan_and_selection_keep_90_10_same_total_across_tenants(self):
        program="""import { arrivalOptions, selectRequest } from './load/protocol.mjs';
const plan=arrivalOptions({rps:25,warmup:300,measure:600,vus:100,maxVus:400});
const rows=[];for(let n=0;n<400;n++)rows.push(selectRequest('multi',n,4,8));
console.log(JSON.stringify({plan,rows}));"""
        result=subprocess.run([os.environ.get('NODE_BINARY','node'),'--input-type=module','-e',program],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,'Arrival protocol must execute')
        data=json.loads(result.stdout); scenarios=data['plan']['scenarios']
        self.assertEqual('constant-arrival-rate',scenarios['measure']['executor'])
        self.assertEqual('300s',scenarios['measure']['startTime'])
        self.assertEqual('600s',scenarios['measure']['duration'])
        self.assertEqual(25,scenarios['measure']['rate'])
        for scenario in scenarios.values():
            self.assertGreaterEqual(int(scenario['gracefulStop'][:-1]),36,
                'The last admitted write must drain two 3s fixture calls and a 30s business timeout')
        self.assertEqual(40,sum(row['operation']=='write' for row in data['rows']))
        for tenant in range(4):
            rows=[row for row in data['rows'] if row['tenant']==tenant]
            self.assertEqual(100,len(rows));self.assertEqual(10,sum(row['operation']=='write' for row in rows))

    def test_fixture_http_requires_capability_and_reserves_unique_events(self):
        fixtures=module(self,'load.fixtures');api=FixtureRenewalTest().client()
        feeder=fixtures.Feeder(api,[{'tenant':'acme','reader':'alice','writer':'writer'}],1,'test',128)
        feeder.tick(1791115200)
        tokens={('acme','alice'):'a'*64,('acme','writer'):'w'*64}
        self.assertTrue(hasattr(fixtures,'FixtureServer'),'Missing fixture HTTP capability service')
        with fixtures.FixtureServer(feeder,tokens,port=0) as server:
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(server.url+'/snapshot')
            self.assertEqual(403,rejected.exception.code)
            headers={'X-Fixture-Capability':server.capability,'Content-Type':'application/json'}
            req=urllib.request.Request(server.url+'/snapshot',headers=headers)
            with urllib.request.urlopen(req) as response:document=json.load(response)
            self.assertEqual('a'*64,document['tenants'][0]['reader_token'])
            payload=json.dumps({'tenant':'acme','index':0}).encode()
            sequences=[]
            for _ in range(2):
                request=urllib.request.Request(server.url+'/reserve',data=payload,headers=headers)
                with urllib.request.urlopen(request) as response:sequences.append(json.load(response)['event']['sequence'])
            self.assertEqual([2,3],sequences)

    def test_resource_sampler_keeps_client_and_both_api_observations(self):
        runner=module(self,'load.runner')
        self.assertTrue(hasattr(runner,'client_sample'),'Missing load generator resource sampling')
        sample=runner.client_sample(__import__('os').getpid())
        self.assertGreater(sample['rss_bytes'],0)
        self.assertGreaterEqual(sample['cpu_seconds'],0)
        self.assertIn('host_memory_available_bytes',sample)

    def test_cli_matrix_plan_does_not_execute_or_shorten_formal_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            frozen=pathlib.Path(directory)/'frozen.json'
            frozen.write_text(json.dumps({'steps':{'low':10,'rated':50,'overload':100}}))
            output=pathlib.Path(directory)/'plan.json'
            result=subprocess.run([sys.executable,'-m','load.runner','plan','--freeze',str(frozen),
                                   '--output',str(output)],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stdout)
            self.assertTrue(output.exists(),'Matrix CLI must emit a reviewable plan')
            data=json.loads(output.read_text())
            self.assertEqual(36,len(data['jobs']))
            self.assertEqual(24840,sum(j['warmup_seconds']+j['measure_seconds'] for j in data['jobs']))


if __name__ == '__main__':
    unittest.main()
