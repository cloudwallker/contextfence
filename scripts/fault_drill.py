#!/usr/bin/env python3
"""Scoped synthetic failure drills. Run each independently; output contains no bodies or tokens."""
import argparse
import csv
import datetime as dt
import errno
import http.client
import http.server
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import uuid

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from ops_common import Ops, OpsError, atomic_json, atomic_bytes, utc_now, gate_is_open
from load.fixtures import IngressClient, load_identities, timestamp
from load.runner import ResourceSampler,endpoints
from ops_smoke import valid_receipt,valid_uuid


def validated_read(response,context,canary,source,expected_status=200):
    """Return only fixed local decisions after the complete fixture contract is checked."""
    status=response.get('status');body=response.get('body')
    status=status if type(status) is int and 0<=status<=599 else 0
    passed=False;decision='INVALID_RESPONSE'
    if isinstance(body,dict):
        if expected_status==200:
            passed=status==200 and set(body)=={'status','code','items','receipt'} and body.get('status')==200 and \
                body.get('code')=='ALLOWED' and body.get('items')==[{'id':context,'content':canary}] and \
                valid_receipt(body.get('receipt'),[context],'ALLOWED',[{'source_id':source,'content_version':1,'auth_epoch':1}])
            if passed:decision='ALLOWED'
        elif expected_status==503:
            passed=status==503 and body=={'code':'DATABASE_UNAVAILABLE'}
            if passed:decision='DATABASE_UNAVAILABLE'
    return {'status':status,'decision':decision,'passed':bool(passed)}


def observe_removal(drill,instance,begin,timeout=15):
    """A slow business HTTP request cannot consume the HAProxy observation deadline."""
    while time.monotonic()-begin<=timeout:
        raw=drill.ops.proxy_command('show stat')
        rows=csv.DictReader(io.StringIO(raw.removeprefix('# ')))
        state=next((row for row in rows if row.get('pxname')=='contextfence' and row.get('svname')==instance),{})
        elapsed=time.monotonic()-begin
        status=state.get('status','');check=state.get('check_status','')
        drill.event('haproxy-detection-sample',instance=instance,
            backend_state='DOWN' if status.startswith('DOWN') else 'UP' if status.startswith('UP') else 'OTHER',
            check='CONNECTION_TIMEOUT' if 'L4TOUT' in check else 'OTHER',detection_seconds=round(elapsed,3))
        if status.startswith('DOWN') and elapsed<=timeout:
            drill.event('haproxy-removed-instance',instance=instance,removal_seconds=round(elapsed,3));return
        time.sleep(.25)
    raise OpsError('HAProxy did not remove killed instance within 15 seconds')


def wait_ingress_backends(ops,timeout=30):
    """Recreated JVM readiness precedes HAProxy DNS/health convergence; establish the baseline first."""
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        rows=list(csv.DictReader(io.StringIO(ops.proxy_command('show stat').removeprefix('# '))))
        if all(any(row.get('pxname')=='contextfence' and row.get('svname')==node and row.get('status')=='UP'
            for row in rows) for node in ('api-a','api-b')):return
        time.sleep(.25)
    raise OpsError('Recreated API backends did not converge to a healthy ingress baseline')


def require_project_container(document,project):
    if document.get('Config',{}).get('Labels',{}).get('com.docker.compose.project')!=project:
        raise OpsError('Fault injection target does not belong to this Compose project')


def verify_scope(ops):
    project=ops.env().get('OPS_PROJECT_NAME','contextfence')
    database=ops.database_service()
    for service in ('api-a','api-b',database,'proxy'):
        container=ops.compose('ps','--quiet',service).stdout.strip()
        if not container:raise OpsError('Fault target is not running')
        data=json.loads(ops.run(['docker','inspect',container]).stdout)[0]
        require_project_container(data,project)
        if service in ('api-a','api-b',database) and data['HostConfig'].get('PortBindings'):
            raise OpsError('Fault environment permits an unsafe direct service port')
    if not gate_is_open(ops.proxy_gate):raise OpsError('Fault baseline requires previously verified open ingress')


class DatabaseCut:
    """Revoke new runtime logins and terminate all current runtime connections, only in this DB."""
    def __init__(self,ops):self.ops=ops
    def __enter__(self):
        self.ops.database('admin','ALTER ROLE cf_runtime NOLOGIN;')
        try:
            self.ops.database('admin',"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() "
                "AND usename='cf_runtime' AND pid<>pg_backend_pid();")
        except Exception:
            self.ops.database('admin','ALTER ROLE cf_runtime LOGIN;');raise
        return self
    def __exit__(self,*args):self.ops.database('admin','ALTER ROLE cf_runtime LOGIN;')


class NetworkDatabaseRoute:
    """Temporarily route this project's API/database monitor through a dedicated TCP proxy."""
    def __init__(self,ops,observe=None):
        self.ops=ops;self.path=ops.runtime/'database.env';self.original=self.path.read_bytes()
        self.target=ops.database_host();self.active_service=ops.database_service();self.routed=False
        self.observe=observe or (lambda phase,**values:None)
    def control(self,action=None):
        if action not in (None,'cut','restore'):raise OpsError('Unsupported proxy control action')
        path='/health' if action is None else '/'+action
        program="import json,urllib.request;request=urllib.request.Request('http://127.0.0.1:18888"+path+"',method='"+('GET' if action is None else 'POST')+"');print(urllib.request.urlopen(request,timeout=3).read().decode())"
        return json.loads(self.ops.compose('exec','-T','db-fault-proxy','python','-c',program,timeout=10).stdout)
    def __enter__(self):
        if self.target=='db-fault-proxy':raise OpsError('A previous DB proxy drill still requires inspection')
        container=self.ops.compose('ps','--quiet',self.target).stdout.strip()
        if not container:raise OpsError('Upstream database is not a running project service')
        require_project_container(json.loads(self.ops.run(['docker','inspect',container]).stdout)[0],self.ops.env().get('OPS_PROJECT_NAME','contextfence'))
        try:
            self.observe('database-proxy-starting')
            self.ops.compose('up','--detach','--wait','--wait-timeout','30','--no-build','db-fault-proxy',
                env={'DB_FAULT_TARGET_HOST':self.target},timeout=60)
        except Exception:
            self.ops.compose('stop','db-fault-proxy',check=False,timeout=30);raise
        proxy_id=self.ops.compose('ps','--quiet','db-fault-proxy').stdout.strip()
        require_project_container(json.loads(self.ops.run(['docker','inspect',proxy_id]).stdout)[0],self.ops.env().get('OPS_PROJECT_NAME','contextfence'))
        if self.control()['blocked']:self.control('restore')
        self.observe('database-proxy-ready')
        try:
            atomic_bytes(self.path,b'DB_HOST=db-fault-proxy\n');self.routed=True
            self.ops.compose('up','--detach','--no-deps','--no-build','--force-recreate','api-a','api-b','postgres-exporter',timeout=180)
            for node in ('api-a','api-b'):self.ops.wait_ready(node,120)
            self.observe('apis-ready-through-database-proxy')
            wait_ingress_backends(self.ops)
            self.observe('haproxy-baseline-ready-through-database-proxy')
            return self
        except Exception:self.__exit__(None,None,None);raise
    def cut(self):
        self.probe_new_connection(0)
        self.observe('network-fault-injection-started')
        result=self.control('cut')
        self.observe('network-fault-injected')
        self.observe('automatic-proxy-and-pg-diagnosis-started')
        if result.get('blocked') is not True or result.get('closed_connections',0)<2:
            raise OpsError('Proxy did not prove interruption of existing API DB connections')
        self.probe_new_connection(2)
        self.observe('automatic-network-cause-confirmed',proxy_blocked=True,
            closed_connections=result['closed_connections'],new_libpq_connection_rejected=True)
        return result
    def probe_new_connection(self,expected_exit):
        # A missing container/psql/secret is not proof that the network proxy rejected a connection.
        program="printf 'CF_DB_PROBE_STARTED\\n'; sh /ops/client.sh runtime >/dev/null 2>&1; result=$?; printf 'CF_DB_PROBE_EXIT=%s\\n' \"$result\"; exit 0"
        result=self.ops.compose('exec','-T','-e','PGHOST=db-fault-proxy','-e','PGCONNECT_TIMEOUT=3',
            self.active_service,'sh','-c',program,input='select 1;\n',check=False,timeout=8)
        if result.returncode!=0 or result.stdout!='CF_DB_PROBE_STARTED\nCF_DB_PROBE_EXIT='+str(expected_exit)+'\n':
            raise OpsError('Database connection probe did not prove its expected libpq result')
    def restore(self):
        self.observe('database-repair-started')
        if self.control('restore').get('blocked') is not False:raise OpsError('DB proxy remained blocked')
        self.observe('database-repair-completed')
    def __exit__(self,*args):
        try:self.restore()
        finally:
            if self.routed:
                atomic_bytes(self.path,self.original)
                self.ops.compose('up','--detach','--no-deps','--no-build','--force-recreate','api-a','api-b','postgres-exporter',timeout=180)
            self.ops.compose('stop','db-fault-proxy',check=False,timeout=30)


class LostResponse:
    """A local client-side last-hop shim: wait for the upstream commit reply, then discard it."""
    def __init__(self,forward):
        self.forward=forward;self.result=None;self.completed=threading.Event();owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if self.path!='/source-event' or not 0<length<=131072:raise ValueError()
                    payload=json.loads(self.rfile.read(length));owner.result=owner.forward(payload)
                except Exception:owner.result=None
                finally:
                    owner.completed.set();self.close_connection=True
                    try:self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    self.connection.close()
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.url='http://127.0.0.1:'+str(self.server.server_port)+'/source-event'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)


class Drill:
    def __init__(self,ops,scenario):
        self.ops=ops;self.scenario=scenario;self.started=time.monotonic();self.run_id=uuid.uuid4().hex
        self.rows=[];self.source='drill.'+self.run_id;self.canary='SYNTHETIC-DRILL-'+self.run_id
        self.token_map,tenants=load_identities(ops.root/'.local/identities.json')
        base=endpoints(ops)[0]
        self.client=IngressClient(base,self.token_map,port=urllib.parse.urlparse(base).port)
        self.tenant=next((row for row in tenants if row['tenant']=='acme'),None)
        if not self.tenant:raise OpsError('Synthetic drill tenant unavailable')
        self.original={'source_id':self.source,'sequence':1,'content':self.canary,'readers':[self.tenant['reader']],
            'state':'ACTIVE','fresh_until':timestamp(time.time()+240)}
        self.context=None
        self.recorded=False
    def event(self,phase,**values):
        self.rows.append({'phase':phase,'recorded_at':utc_now(),'elapsed_seconds':round(time.monotonic()-self.started,3),**values})
    def post(self,path,payload,writer=False):
        return self.client.post(self.tenant['tenant'],self.tenant['writer' if writer else 'reader'],path,payload)
    def seed(self):
        self.record_original()
        result=self.post('/v1/source-events',self.original,True)
        if result['status']!=200 or result['body']!={'source_id':self.source,'sequence':1,'outcome':'APPLIED','content_version':1,'auth_epoch':1}:raise OpsError('Drill source registration failed')
        context=self.post('/v1/contexts/source',{'source_id':self.source,'ttl_seconds':180})
        if context['status']!=201:raise OpsError('Drill context registration failed')
        self.context=context['body'].get('id')
        if not valid_uuid(self.context):raise OpsError('Drill context response was malformed')
        self.read_allowed();self.read_allowed();self.event('baseline-permission-and-warm-cache-passed')
    def record_original(self):
        if not self.recorded:
            from fixture_producer import append_event
            append_event(self.ops.root/'.local/recovery/ledger.json',self.tenant['tenant'],self.original)
            self.recorded=True
    def read_allowed(self):
        begin=time.perf_counter()
        try:response=self.post('/v1/contexts/assemble',{'context_ids':[self.context]})
        except ValueError:
            self.event('read',status=0,decision='TRANSPORT_FAILURE',passed=False,e2e_ms=round((time.perf_counter()-begin)*1000,3))
            raise OpsError('Synthetic allowed request transport failed') from None
        result=validated_read(response,self.context,self.canary,self.source)
        self.event('read',**result,e2e_ms=round((time.perf_counter()-begin)*1000,3))
        if not result['passed']:raise OpsError('Synthetic allowed request failed')
    def safety(self):
        for node in ('api-a','api-b'):
            self.ops.wait_ready(node,120);self.ops.smoke(node)
        self.event('recovery-dual-instance-safety-passed')
    def alert(self,name,status,since,timeout=60,instance=None):
        started=dt.datetime.fromisoformat(since);deadline=started.timestamp()+timeout
        while True:
            path=self.ops.root/'.local/alerts/alerts.jsonl'
            if path.exists():
                for line in path.read_text(encoding='utf-8').splitlines():
                    record=json.loads(line)
                    received=dt.datetime.fromisoformat(record['recorded_at']).timestamp()
                    if not started.timestamp()<=received<=deadline:continue
                    for alert in record.get('alerts',[]):
                        labels=alert.get('labels',{})
                        if labels.get('alertname')==name and alert.get('status')==status and (instance is None or labels.get('instance')==instance):
                            self.event('alert-'+status,alert=name,instance=instance,received_at=record['recorded_at'],
                                delivery_seconds=round(received-started.timestamp(),3));return
            if time.time()>=deadline:break
            time.sleep(.5)
        raise OpsError('Expected local alert was not observed before deadline')
    def report(self,passed):
        return {'format_version':1,'scenario':self.scenario,'run_id':self.run_id,'recorded_at':utc_now(),
            'runtime_passed':passed,'duration_seconds':round(time.monotonic()-self.started,3),'observations':self.rows}


def instance_kill(drill,instance):
    drill.seed();started=utc_now();begin=time.monotonic();drill.ops.compose('kill','--signal','SIGKILL',instance)
    drill.event('instance-killed',instance=instance)
    stop=threading.Event()
    def traffic():
        while not stop.is_set():
            try:drill.read_allowed()
            except (OpsError,ValueError):pass
            stop.wait(.25)
    client=threading.Thread(target=traffic,daemon=True);client.start()
    try:
        observe_removal(drill,instance,begin)
        for _ in range(3):drill.read_allowed()
        survivor='api-b' if instance=='api-a' else 'api-a';drill.ops.smoke(survivor)
        drill.alert('ApiUnavailable','firing',started,60,instance)
    finally:
        stop.set();restored=utc_now();drill.ops.compose('up','--detach','--no-deps','--no-build',instance,timeout=180)
        client.join(timeout=35)
    drill.safety();drill.alert('ApiUnavailable','resolved',restored,90,instance)


def database_disconnect(drill):
    with NetworkDatabaseRoute(drill.ops,drill.event) as proxy:
        drill.seed();started=utc_now();result=proxy.cut()
        drill.event('database-proxy-cut-existing-and-new-connections',closed_connections=result['closed_connections'])
        for node in ('api-a','api-b'):
            deadline=time.monotonic()+20
            while True:
                response=drill.ops.internal_http(node,'GET','/health/ready',principal=None)
                if response['status']==503:
                    drill.event('readiness-failed',instance=node,status=503);break
                if time.monotonic()>deadline:raise OpsError('Database loss did not fail readiness')
                time.sleep(.5)
            reply=drill.ops.internal_http(node,'POST','/v1/contexts/assemble',{'context_ids':[drill.context]},('acme','alice'))
            result=validated_read(reply,drill.context,drill.canary,drill.source,503)
            drill.event('db-loss-cache-failclosed',instance=node,**result)
            if not result['passed']:raise OpsError('Warm cached context was unsafe during DB loss')
        drill.alert('ApiUnavailable','firing',started,60)
        restored=utc_now();proxy.restore();drill.event('database-connections-restored');drill.safety();drill.read_allowed()
        drill.alert('ApiUnavailable','resolved',restored,90)
    drill.safety();drill.event('original-database-route-restored')


def commit_response_loss(drill):
    drill.record_original()
    def forward(payload):return drill.post('/v1/source-events',payload,True)
    with LostResponse(forward) as loss:
        request=urllib.request.Request(loss.url,data=json.dumps(drill.original).encode(),headers={'Content-Type':'application/json'})
        lost=False
        try:urllib.request.urlopen(request,timeout=35)
        except (http.client.RemoteDisconnected,urllib.error.URLError,ConnectionError):lost=True
        if not lost or not loss.completed.wait(5) or loss.result is None or loss.result['status']!=200:
            raise OpsError('Could not prove source commit before response loss')
        committed=loss.result['body'];drill.event('commit-confirmed-response-discarded',sequence=1)
    replay=forward(drill.original)
    if replay['status']!=200 or replay['body']!=committed:raise OpsError('Identical replay changed persisted event result')
    conflict=forward(dict(drill.original,content=drill.canary+'-DIFFERENT'))
    if conflict['status']!=409 or conflict['body'].get('code')!='EVENT_CONFLICT':raise OpsError('Same-sequence conflict was not rejected')
    count=drill.ops.database('backup',"select count(*) from source_events where tenant='acme' and source_id='"+drill.source+"' and sequence=1;").strip()
    if count!='1':raise OpsError('Response-loss replay duplicated the event')
    drill.event('idempotent-replay-and-sequence-conflict-passed',persisted_event_count=1)


def hotspot_lock(drill):
    drill.seed()
    # Named transaction makes the blocking session identifiable without recording SQL or application data.
    sql="SET application_name='contextfence-drill-lock';BEGIN;SELECT tenant FROM tenant_guard WHERE tenant='acme' FOR UPDATE;SELECT pg_sleep(10);ROLLBACK;\n"
    process=subprocess.Popen(drill.ops.database_args('admin'),cwd=drill.ops.root,stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True)
    try:
        process.stdin.write(sql);process.stdin.flush();process.stdin.close()
        deadline=time.monotonic()+5
        while True:
            active=drill.ops.database('monitor',"select count(*) from pg_stat_activity where application_name='contextfence-drill-lock' and wait_event='PgSleep';").strip()
            if active=='1':break
            if time.monotonic()>deadline:raise OpsError('Hot tenant lock was not acquired')
            time.sleep(.1)
        drill.event('hot-tenant-lock-acquired')
        replies=[]
        def request():
            begin=time.perf_counter()
            try:reply=drill.post('/v1/contexts/assemble',{'context_ids':[drill.context]})
            except ValueError:reply={'status':0,'body':{}}
            result=validated_read(reply,drill.context,drill.canary,drill.source,503 if reply['status']==503 else 200)
            replies.append({**result,'e2e_ms':round((time.perf_counter()-begin)*1000,3)})
        clients=[threading.Thread(target=request) for _ in range(6)]
        for client in clients:client.start()
        time.sleep(1)
        sample=json.loads(drill.ops.database('monitor',"select json_build_object('blocked',count(*) filter(where cardinality(pg_blocking_pids(pid))>0),"
            "'runtime_lock_waiters',count(*) filter(where usename='cf_runtime' and wait_event_type='Lock')) from pg_stat_activity where datname=current_database();").strip())
        if sample['blocked']<1 or sample['runtime_lock_waiters']<1:raise OpsError('PG did not expose the injected lock wait')
        for client in clients:client.join(timeout=35)
        drill.event('pg-lock-blocker-sample',**sample);drill.event('lock-window-requests',samples=replies)
        if len(replies)!=6 or not all(reply['passed'] for reply in replies):raise OpsError('Unexpected response during lock drill')
        process.wait(timeout=15)
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=10)
    drill.read_allowed();drill.safety()


class NoSpaceOps(Ops):
    """Inject actual ENOSPC at a backup's host copy boundary, after the real pg_dump."""
    def compose(self,*args,**kwargs):
        if args and args[0]=='cp':
            if len(args)!=3 or not pathlib.Path(args[2]).resolve().is_relative_to((self.root/'.local/backups').resolve()):
                raise OpsError('Unsafe backup failure injection path')
            with open('/dev/full','wb',buffering=0) as device:device.write(b'SYNTHETIC-ENOSPC')
            raise OpsError('ENOSPC device did not reject the copy')
        return super().compose(*args,**kwargs)


def backup_failure(drill):
    from backup import run_backup,BackupError,BackupStore
    store=BackupStore(drill.ops.root/'.local/backups');latest=store.latest()
    before=(store.root/'latest.json').read_bytes();started=utc_now()
    injected=NoSpaceOps(drill.ops.root)
    try:run_backup(injected)
    except BackupError:pass
    else:raise OpsError('ENOSPC backup unexpectedly succeeded')
    if (store.root/'latest.json').read_bytes()!=before or store.latest()!=latest:raise OpsError('Failed backup replaced last valid backup')
    drill.event('backup-enospc-failed-latest-preserved')
    drill.alert('BackupFailed','firing',started,90)
    restored=utc_now();run_backup(drill.ops);drill.event('backup-successful-after-injection')
    drill.alert('BackupFailed','resolved',restored,90)


def validate_bad_release_report(report,candidate,previous):
    if report.get('outcome')!='ROLLED_BACK' or report.get('candidate_digest')!=candidate or report.get('previous_digests')!=previous:
        raise OpsError('Bad release did not restore the intended previous release')
    duration=report.get('duration_seconds')
    if type(duration) not in (int,float) or not 0<=duration<=300:
        raise OpsError('Bad release exceeded its complete rollback acceptance deadline')
    verification=report.get('verification',{})
    maven=verification.get('maven',{});python=verification.get('python',{})
    if maven.get('exit_code')!=0 or python.get('exit_code')!=0 or type(python.get('tests')) is not int or python['tests']<=0:
        raise OpsError('Bad release lacks actual mandatory verification evidence')
    for kind in ('unit','integration'):
        checks=maven.get(kind,{})
        if type(checks.get('tests')) is not int or checks['tests']<=0 or any(checks.get(key)!=0 for key in ('failures','errors','skipped')):
            raise OpsError('Bad release lacks completed real Maven verification')
    rows=report.get('observations',[])
    if any(row.get('phase')=='candidate-in-flow' for row in rows):
        raise OpsError('Bad candidate received ordinary business traffic')
    required=[('migration-complete',None),('previous-schema-safety-passed','api-b'),('drained','api-a'),
        ('candidate-started','api-a'),('candidate-readiness-failed','api-a'),('rollback-ingress-closed',None),
        ('rollback-safety-passed','api-a'),('rollback-safety-passed','api-b'),('rollback-accepted',None)]
    offset=0
    for phase,instance in required:
        match=next((i for i in range(offset,len(rows)) if (rows[i].get('phase'),rows[i].get('instance'))==(phase,instance)),None)
        if match is None:raise OpsError('Bad release did not exercise the complete readiness failure and rollback path')
        if phase=='candidate-readiness-failed':
            forward=rows[match].get('elapsed_seconds')
            if type(forward) not in (int,float) or not 0<=forward<=120:
                raise OpsError('Bad release exceeded its forward readiness acceptance deadline')
        offset=match+1


def bad_release(drill,candidate):
    if not candidate:raise OpsError('Bad-release drill needs a locally built incompatible candidate image')
    image=json.loads(drill.ops.run(['docker','image','inspect',candidate]).stdout)[0]['Id']
    manifest=drill.ops.image_manifest();previous={name:row['image_digest'] for name,row in manifest['services'].items()}
    output=drill.ops.root/'artifacts/local'/('bad-release-pipeline-'+drill.run_id+'.json')
    result=drill.ops.run([sys.executable,str(drill.ops.root/'scripts/deploy.py'),'--root',str(drill.ops.root),
        '--candidate',image,'--output',str(output)],check=False,timeout=1500)
    drill.event('mandatory-release-pipeline-completed',exit_code=result.returncode,evidence=output.name)
    if result.returncode!=1 or not output.exists():raise OpsError('Mandatory release pipeline did not reach rollback')
    report=json.loads(output.read_text())
    drill.event('bad-release-outcome',deployment=report)
    validate_bad_release_report(report,image,previous)
    actual=drill.ops.image_manifest()['services']
    if any(actual[name]['image_digest']!=digest for name,digest in previous.items()):raise OpsError('Rollback left a candidate image active')
    drill.event('rollback-actual-digests-verified',actual_digests={name:row['image_digest'] for name,row in actual.items()},
        previous_digests=previous,duration_seconds=report['duration_seconds'])
    drill.safety()


def material_anomaly(drill,backup,ledger,fixture,output):
    from restore import run_restore,RestoreError,validate_ledger
    if not backup or not ledger or not fixture:raise OpsError('Material anomaly requires valid backup, ledger and fixture inputs')
    original=pathlib.Path(ledger).read_text(encoding='utf-8');validate_ledger(original)
    document=json.loads(original);document['events_sha256']='0'*64
    damaged=pathlib.Path(output).with_suffix('.invalid-ledger.json');atomic_json(damaged,document)
    failed=False
    try:run_restore(drill.ops,pathlib.Path(backup),damaged,pathlib.Path(fixture),utc_now())
    except RestoreError:failed=True
    if not failed or gate_is_open(drill.ops.proxy_gate) or gate_is_open(drill.ops.app_gate):
        raise OpsError('Invalid recovery material did not preserve both closed gates')
    drill.ops.verify_proxy_closed()
    # Expected anomaly intentionally remains closed; accepting the drill never opens traffic.
    drill.event('damaged-ledger-rejected-both-gates-remain-closed',ingress_left_closed=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario',choices=['instance-kill','database-disconnect','commit-response-loss','hotspot-lock','bad-release','material-anomaly','backup-failure'])
    parser.add_argument('--root',default=str(ROOT));parser.add_argument('--instance',choices=['api-a','api-b'],default='api-a')
    parser.add_argument('--candidate');parser.add_argument('--output',required=True)
    parser.add_argument('--backup');parser.add_argument('--ledger');parser.add_argument('--fixture')
    args=parser.parse_args();ops=Ops(args.root);drill=None
    try:
        if os.name!='posix':raise OpsError('Runtime fault drills require the fixed Linux environment')
        output=pathlib.Path(args.output).resolve()
        if not output.is_relative_to((ops.root/'artifacts/local').resolve()) or output.exists():raise OpsError('Fault evidence needs a new ignored local path')
        verify_scope(ops);drill=Drill(ops,args.scenario)
        resources=output.with_suffix('.resources.jsonl');resources.parent.mkdir(parents=True,exist_ok=True)
        with ResourceSampler(ops,os.getpid(),resources) as sampler:
            if args.scenario=='instance-kill':instance_kill(drill,args.instance)
            elif args.scenario=='database-disconnect':database_disconnect(drill)
            elif args.scenario=='commit-response-loss':commit_response_loss(drill)
            elif args.scenario=='hotspot-lock':hotspot_lock(drill)
            elif args.scenario=='bad-release':bad_release(drill,args.candidate)
            elif args.scenario=='material-anomaly':material_anomaly(drill,args.backup,args.ledger,args.fixture,output)
            else:backup_failure(drill)
        report=drill.report(True);report.update({'resource_evidence':resources.name,'resource_sampling_errors':sampler.errors,
            'sampling_failure_during_injected_outage_is_retained':True})
        atomic_json(output,report);print('PASS: synthetic fault drill '+args.scenario);return 0
    except (OpsError,OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired):
        if drill is not None:
            try:ops.close_maintenance('fault-drill-failed')
            except OpsError:pass
            drill.event('fault-acceptance-failed-ingress-closed');atomic_json(pathlib.Path(args.output),drill.report(False))
        print('FAIL: synthetic fault drill; private bodies and credentials were not printed');return 1


if __name__=='__main__':raise SystemExit(main())
