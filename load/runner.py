"""Run probes, freeze a reviewable target, then execute the immutable formal matrix."""
import hashlib
import json
import os
import pathlib
import sys
import argparse
import copy
import concurrent.futures
import ctypes
import platform
import re
import subprocess
import threading
import time
import urllib.request
import urllib.parse

if __package__ in (None,''):
    sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from load.fixtures import LoadError, Feeder, FixtureServer, IngressClient, load_identities, timestamp
from load.analyze import EvidenceError, summarize
from load.telemetry import LoadCounters,RawLoadExporter
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from ops_common import Ops, OpsError, atomic_json, atomic_bytes


def endpoints(ops):
    configuration=ops.env();project=configuration.get('OPS_PROJECT_NAME','contextfence')
    pair=(int(configuration.get('ENTRY_PORT','58090')),int(configuration.get('PROMETHEUS_PORT','59090')))
    if pair==(58095,59095):
        if project!='contextfence-bench':raise LoadError('Isolated load ports require the bench Compose project')
    elif pair!=(58090,59090):raise LoadError('Load endpoints are not an approved HAProxy/Prometheus pair')
    return ('http://127.0.0.1:'+str(pair[0]),'http://127.0.0.1:'+str(pair[1]))


def formal_jobs(frozen):
    jobs=[]
    for scenario in ('cold','hot','single','multi'):
        for step,rps in frozen['steps'].items():
            for repeat in (1,2,3):
                jobs.append({'kind':'formal','scenario':scenario,'step':step,'rps':rps,'repeat':repeat,
                    'warmup_seconds':0 if scenario=='cold' else 300,
                    'measure_seconds':60 if scenario=='cold' else 600})
    return jobs


def remaining_jobs(jobs,reports):
    if len(reports)>len(jobs):raise LoadError('Resume contains more runs than the frozen protocol')
    for job,report in zip(jobs,reports):
        if any(report.get(key)!=value for key,value in job.items()):
            raise LoadError('Completed runs are not a prefix of this frozen matrix')
    return jobs[len(reports):]


def verify_comparison(baseline,candidate,change):
    if change.get('parameter')!='connection_pool_maximum_size' or change.get('configuration_reviewed') is not True or \
       type(change.get('before')) is not int or type(change.get('after')) is not int or \
       not 1<=change['after']<=64 or change['before']==change['after']:
        raise LoadError('Comparison requires one reviewed connection-pool change')
    for environment_document,value in ((baseline,change['before']),(candidate,change['after'])):
        if environment_document.get('application_pool_max')!={'api-a':value,'api-b':value}:
            raise LoadError('Observed API pool sizes do not match the declared optimization')
    before=copy.deepcopy(baseline);after=copy.deepcopy(candidate)
    for document in (before,after):
        document.pop('application_pool_max',None)
        document['images'].pop('public_config_sha256',None)
    if before!=after:raise LoadError('Optimization also changed an image, resource, client, script or environment')


def freeze(path,probes,resource_evidence,low,rated,overload,environment,tolerance=.20):
    path=pathlib.Path(path)
    if path.exists(): raise LoadError('Frozen target already exists; preserve it for regression comparison')
    if not 0<low<rated<overload or not 0<=tolerance<=.50 or resource_evidence.get('resource_stable') is not True:
        raise LoadError('Invalid target or missing resource review')
    indexed={(p['scenario'],p['rps']):p for p in probes}
    if any((s,r) not in indexed for s in ('hot','single','multi') for r in (10,25,50,100,200)):
        raise LoadError('All five initial arrival-rate probes are required')
    if any(not indexed.get((s,r),{}).get('capacity_eligible') for s in ('hot','single','multi') for r in (low,rated)):
        raise LoadError('Low or rated target did not pass all scenario probes')
    if any((s,overload) not in indexed for s in ('hot','single','multi')):
        raise LoadError('Overload observation step must have probe evidence')
    tails={name:max(indexed[(s,rated)]['latency_ms'][name] for s in ('hot','single','multi')) for name in ('p95','p99')}
    document={'format_version':1,'steps':{'low':low,'rated':rated,'overload':overload},
        'baseline_latency_ms':tails,'regression_tolerance':tolerance,
        'latency_limits_ms':{name:value*(1+tolerance) for name,value in tails.items()},
        'resource_review':resource_evidence,'environment':environment,
        'protocol':{'steady_warmup_seconds':300,'steady_measure_seconds':600,'repeats':3,'cold_measure_seconds':60},
        'dataset':{'version':'synthetic-v1','body_bytes':1024,'derived_depth':1,'batch_size':2,'sources_per_tenant':8}}
    path.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create makes accidental reruns unable to rewrite the accepted target.
    with path.open('x',encoding='utf-8') as stream: json.dump(document,stream,indent=2); stream.write('\n')
    return document


def client_sample(pid):
    """Read process CPU/RSS and available host memory without environment or command line."""
    if os.name=='posix':
        proc=pathlib.Path('/proc')
        fields=(proc/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
        cpu=(int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
        status=dict(line.split(':',1) for line in (proc/str(pid)/'status').read_text().splitlines() if ':' in line)
        memory=dict(line.split(':',1) for line in (proc/'meminfo').read_text().splitlines() if ':' in line)
        return {'pid':pid,'cpu_seconds':cpu,'rss_bytes':int(status['VmRSS'].split()[0])*1024,
            'host_memory_available_bytes':int(memory['MemAvailable'].split()[0])*1024,
            'host_cpu_ticks':list(map(int,(proc/'stat').read_text().splitlines()[0].split()[1:]))}
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in
            ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
             'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    class Memory(ctypes.Structure):
        _fields_=[('dwLength',wintypes.DWORD),('dwMemoryLoad',wintypes.DWORD)]+[(name,ctypes.c_ulonglong) for name in
            ('ullTotalPhys','ullAvailPhys','ullTotalPageFile','ullAvailPageFile','ullTotalVirtual','ullAvailVirtual','ullAvailExtendedVirtual')]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    handle=kernel.OpenProcess(0x410,False,pid)
    if not handle:raise LoadError('Client resource observation unavailable')
    try:
        counters=Counters();counters.cb=ctypes.sizeof(counters)
        psapi=ctypes.WinDLL('psapi',use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        times=[wintypes.FILETIME() for _ in range(4)]
        memory=Memory();memory.dwLength=ctypes.sizeof(memory)
        if not psapi.GetProcessMemoryInfo(handle,ctypes.byref(counters),counters.cb) or \
           not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in times]) or \
           not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):raise LoadError('Client resource observation failed')
        cpu=sum((value.dwHighDateTime<<32)+value.dwLowDateTime for value in times[2:])/10**7
        return {'pid':pid,'cpu_seconds':cpu,'rss_bytes':counters.WorkingSetSize,'host_memory_available_bytes':memory.ullAvailPhys}
    finally:kernel.CloseHandle(handle)


def process_status_rss(raw) -> int:
    """Return only RSS bytes from the uniquely selected Java child of PID 1."""
    invalid='Java init-child RSS observation unavailable or invalid'
    if type(raw) is not str or not raw or len(raw)>65536:raise LoadError(invalid)
    fields={};required=('ContextFenceJavaPid','Name','Pid','PPid','VmRSS')
    for line in raw.splitlines():
        key,separator,value=line.partition(':');key=key.strip()
        if key in required:
            if not separator or key in fields:raise LoadError(invalid)
            fields[key]=value.strip()
        elif re.match(r'^(ContextFenceJavaPid|Name|Pid|PPid|VmRSS)([ \t]|$)',key):raise LoadError(invalid)
    marker=fields.get('ContextFenceJavaPid','')
    if re.fullmatch(r'[1-9][0-9]{0,9}',marker) is None or not 1<int(marker)<(1<<31):raise LoadError(invalid)
    if fields.get('Name')!='java' or fields.get('Pid')!=marker or fields.get('PPid')!='1':raise LoadError(invalid)
    rss=re.fullmatch(r'([1-9][0-9]{0,15})[ \t]+kB',fields.get('VmRSS',''))
    if rss is None:raise LoadError(invalid)
    kibibytes=int(rss.group(1))
    if kibibytes>((1<<63)-1)//1024:raise LoadError(invalid)
    return kibibytes*1024


def read_api_process_rss(ops,node) -> int:
    """Observe one fixed API's unique init child, without environment or command line."""
    if type(node) is not str or node not in ('api-a','api-b'):raise LoadError('Invalid API RSS observation target')
    command=('set -euf; children=$(cat /proc/1/task/1/children); set -- $children; '
        '[ "$#" -eq 1 ]; pid=$1; case "$pid" in \'\'|*[!0-9]*) exit 1;; esac; '
        '[ "$pid" -gt 1 ]; printf \'ContextFenceJavaPid:%s\\n\' "$pid"; cat "/proc/$pid/status"')
    result=ops.compose('exec','-T',node,'sh','-c',command,timeout=5)
    return process_status_rss(result.stdout)


class ResourceSampler:
    def __init__(self,ops,pid,path):
        self.ops=ops;self.pid=pid;self.path=path;self.stop=threading.Event();self.errors=0
        self.thread=threading.Thread(target=self.loop,daemon=True)
    def _containers(self):
        container_ids=self.ops.compose('ps','--quiet','api-a','api-b',self.ops.database_service(),timeout=15).stdout.split()
        if len(container_ids)!=3:raise LoadError('API/DB observation unavailable')
        result=self.ops.run(['docker','stats','--no-stream','--format','{{json .}}']+container_ids,timeout=15)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    def _database(self):
        # Select aggregate metadata only; SQL statements/subject/source values are not recorded.
        return json.loads(self.ops.database('monitor',
            "select json_build_object('connections',count(*),'waiting',count(*) filter(where wait_event_type='Lock'),"
            "'active',count(*) filter(where state='active'),'oldest_wait_seconds',coalesce(max(extract(epoch from "
            "clock_timestamp()-query_start)) filter(where wait_event_type='Lock'),0)) from pg_stat_activity "
            "where datname=current_database() and usename='cf_runtime';").strip())
    def _metrics(self):
        query='{__name__=~"hikaricp_connections_.*|process_cpu_usage|process_resident_memory_bytes|jvm_memory_used_bytes|jvm_gc_pause_seconds_.*|node_cpu_seconds_total|node_memory_.*|node_disk_.*|node_filesystem_.*|pg_stat_database_.*|pg_locks_count"}'
        with urllib.request.urlopen(endpoints(self.ops)[1]+'/api/v1/query?'+urllib.parse.urlencode({'query':query}),timeout=5) as response:
            document=json.load(response)
        if document.get('status')!='success':raise LoadError('Monitoring query failed')
        return document['data']['result']
    def loop(self):
        while not self.stop.is_set():
            sampling_started=time.monotonic()
            row={'recorded_at':timestamp(time.time())}
            try:row['client']=client_sample(self.pid)
            except (OSError,ValueError,LoadError):row['client_observation_failed']=True;self.errors+=1
            # Five fixed read-only I/O groups overlap; only this coordinator assembles and writes rows.
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                rss_futures={node:executor.submit(read_api_process_rss,self.ops,node) for node in ('api-a','api-b')}
                service_futures={'containers':executor.submit(self._containers),'database':executor.submit(self._database)}
                metrics_future=executor.submit(self._metrics)
                row['api_process']={};failed_instances=[];failure_kinds={}
                for node,future in rss_futures.items():
                    try:row['api_process'][node]={'rss_bytes':future.result(),'source':'java-init-child-proc-status'}
                    except (OSError,ValueError,LoadError,OpsError) as error:
                        failed_instances.append(node)
                        failure_kinds[node]=next(kind for cls,kind in ((OpsError,'ops-error'),(LoadError,'load-error'),
                            (OSError,'os-error'),(ValueError,'value-error')) if isinstance(error,cls))
                if failed_instances:
                    row['api_process_observation_failed']=True;row['api_process_failed_instances']=failed_instances
                    row['api_process_failure_kinds']=failure_kinds;self.errors+=1
                service_failed=False
                for field,future in service_futures.items():
                    try:row[field]=future.result()
                    except (OSError,ValueError,LoadError,OpsError):service_failed=True
                if service_failed:row['service_observation_failed']=True;self.errors+=1
                try:
                    row['metrics']=metrics_future.result()
                    if not all(any(metric['metric'].get('instance')==node for metric in row['metrics']) for node in ('api-a','api-b')):
                        raise LoadError('Both API pool/resource series are required')
                except (OSError,ValueError,KeyError,LoadError):row['metric_observation_failed']=True;self.errors+=1
            with self.path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(row)+'\n')
            self.stop.wait(max(0,5-(time.monotonic()-sampling_started)))
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.stop.set();self.thread.join()


def application_source_identity(root):
    root=pathlib.Path(root)
    files=[root/'pom.xml',root/'Dockerfile']+[path for path in (root/'src/main').rglob('*') if path.is_file()]
    digest=hashlib.sha256()
    for path in sorted(files,key=lambda item:item.relative_to(root).as_posix()):
        if path.is_symlink():raise LoadError('Application source identity requires local regular files')
        name=path.relative_to(root).as_posix().encode('utf-8');content=path.read_bytes()
        digest.update(len(name).to_bytes(8,'big'));digest.update(name)
        digest.update(len(content).to_bytes(8,'big'));digest.update(content)
    return digest.hexdigest()


def environment(ops,k6,vus,max_vus):
    version=ops.run([k6,'version'],timeout=15).stdout.strip()
    manifest=ops.image_manifest()
    manifest.pop('recorded_at',None)
    sha=ops.run(['git','rev-parse','HEAD']).stdout.strip()
    dirty=bool(ops.run(['git','status','--porcelain','--untracked-files=no']).stdout.strip())
    query='hikaricp_connections_max{job="contextfence"}'
    with urllib.request.urlopen(endpoints(ops)[1]+'/api/v1/query?'+urllib.parse.urlencode({'query':query}),timeout=5) as response:
        pool_document=json.load(response)
    pools={row['metric']['instance']:int(float(row['value'][1])) for row in pool_document['data']['result']
           if row['metric'].get('instance') in ('api-a','api-b')}
    if set(pools)!={'api-a','api-b'}:raise LoadError('Observed connection pool maxima are unavailable')
    return {'images':manifest,'application_pool_max':pools,'client':{'os':platform.system(),'kernel':platform.release(),
        'python':platform.python_version(),'k6':version,'cpus':os.cpu_count(),'vus':vus,'max_vus':max_vus},
        'git':{'base_commit':sha,'tracked_tree_dirty':dirty},'application_source_sha256':application_source_identity(ops.root),
        'scripts_sha256':hashlib.sha256(b''.join(path.read_bytes() for path in sorted((ops.root/'load').glob('*')) if path.is_file())).hexdigest()}


def run_job(ops,job,output,k6,vus,max_vus,tenant_count=4):
    output=pathlib.Path(output);output.mkdir(parents=True,exist_ok=False)
    tokens,tenants=load_identities(ops.root/'.local/identities.json')
    if tenant_count<2 or len(tenants)<tenant_count:raise LoadError('Multi-tenant benchmark needs at least two complete tenants')
    tenants=[row for row in tenants if row['tenant']!='ops'][:tenant_count]
    selected=tenants if job['scenario']=='multi' else tenants[:1]
    base=endpoints(ops)[0]
    feeder=Feeder(IngressClient(base,tokens,output/'feeder.jsonl',int(urllib.parse.urlparse(base).port)),selected)
    started=time.time();feeder.tick()  # registration only; no assemble or cache warming
    if job['scenario']=='cold':
        ops.compose('restart','api-a','api-b',timeout=120)
        for node in ('api-a','api-b'):ops.wait_ready(node,120)
        feeder.tick()  # keep SOURCE freshness current while ready checks ran
    stopped=threading.Event()
    with FixtureServer(feeder,tokens) as server:
        def renew():
            while not stopped.wait(1):
                try:feeder.tick()
                except Exception:server.failure=True;return
        renewal=threading.Thread(target=renew,daemon=True);renewal.start()
        raw=output/'raw.jsonl'
        private_env=os.environ.copy();private_env.update({'CF_BASE':base,'CF_FIXTURE_URL':server.url,
            'CF_FIXTURE_CAPABILITY':server.capability,'CF_SCENARIO':job['scenario'],'CF_RPS':str(job['rps']),
            'CF_WARMUP':str(job['warmup_seconds']),'CF_MEASURE':str(job['measure_seconds']),
            'CF_VUS':str(vus),'CF_MAX_VUS':str(max_vus)})
        # Never persist command environment or HTTP response bodies. Built-in samples contain bounded tags only.
        command=[k6,'run','--quiet','--out','json='+str(raw),str(ops.root/'load/k6.js')]
        process=None
        try:
            with (output/'k6-private.log').open('w',encoding='utf-8') as log:
                with RawLoadExporter(raw,job['scenario'],LoadCounters(ops.root/'.local/metrics')) as client_exporter:
                    process=subprocess.Popen(command,cwd=ops.root,env=private_env,stdout=log,stderr=subprocess.STDOUT,shell=False)
                    with ResourceSampler(ops,process.pid,output/'resources.jsonl') as sampler:
                        code=process.wait(timeout=job['warmup_seconds']+job['measure_seconds']+120)
        except (OSError,subprocess.TimeoutExpired):
            if process is not None:process.kill();process.wait()
            atomic_json(output/'execution.json',{'job':job,'k6_exit_code':None if process is None else process.returncode,
                'protocol_completed':False,'started_at':timestamp(started),'ended_at':timestamp(time.time()),
                'reason':'load-process-unavailable-or-timeout'})
            raise LoadError('Load process unavailable or exceeded protocol deadline') from None
        finally:stopped.set();renewal.join(timeout=40)
        ended_at=timestamp(time.time())  # Preserve the load boundary before raw analysis.
        atomic_json(output/'execution.json',{'job':job,'k6_exit_code':code,'protocol_completed':code==0,
            'started_at':timestamp(started),'ended_at':ended_at,'feeder_failed':server.failure is not None,
            'resource_sampling_errors':sampler.errors})
        report=summarize([raw],job['measure_seconds'],job['rps'])
        report.update(job);report.update({'k6_exit_code':code,'started_at':timestamp(started),'ended_at':ended_at,
            'feeder_failed':server.failure is not None,'feeder_reserved_writes':feeder.reserved_writes,
            'fixture_aux_requests':server.aux_requests,'resource_sampling_errors':sampler.errors,
            'client_verdict_export_failed':client_exporter.failure,
            'client_verdict_export_failure_details':client_exporter.failure_details,
            'evidence':{'raw':'raw.jsonl','feeder':'feeder.jsonl','resources':'resources.jsonl'},
            'formal_protocol':job['kind']=='formal'})
        report['capacity_eligible']=report['capacity_eligible'] and code==0 and server.failure is None and sampler.errors==0 and not client_exporter.failure
        atomic_json(output/'summary.json',report)
        return report


def read_json(path):return json.loads(pathlib.Path(path).read_text(encoding='utf-8-sig'))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['policy','probe','shortprobe','freeze','plan','matrix','compare'])
    parser.add_argument('--root',default=str(pathlib.Path(__file__).resolve().parents[1]));parser.add_argument('--output',required=True)
    parser.add_argument('--k6',default='k6');parser.add_argument('--policy');parser.add_argument('--freeze',dest='frozen')
    parser.add_argument('--probes');parser.add_argument('--resource-review');parser.add_argument('--environment')
    parser.add_argument('--low',type=int);parser.add_argument('--rated',type=int);parser.add_argument('--overload',type=int)
    parser.add_argument('--tolerance',type=float,default=.20);parser.add_argument('--vus',type=int,default=512)
    parser.add_argument('--max-vus',type=int,default=2048);parser.add_argument('--tenants',type=int,default=4)
    parser.add_argument('--scenario',choices=['hot','single','multi'],default='hot');parser.add_argument('--rps',type=int,default=10)
    parser.add_argument('--seconds',type=int,default=10);parser.add_argument('--resume',action='store_true')
    parser.add_argument('--change');args=parser.parse_args();ops=Ops(args.root)
    output=pathlib.Path(args.output)
    try:
        if args.action=='policy':
            if not 0<=args.tolerance<=.50:raise LoadError('Invalid regression tolerance')
            output.parent.mkdir(parents=True,exist_ok=True)
            with output.open('x',encoding='utf-8') as stream:json.dump({'format_version':1,'created_at':timestamp(time.time()),
                'regression_tolerance':args.tolerance,'max_unexpected_failure_rate':.01,'max_unsafe_allow':0,
                'required_probe_rps':[10,25,50,100,200],'steady_warmup_seconds':300,'steady_measure_seconds':600,
                'max_sustained_wait_growth_ratio':1.20,'client_vus':args.vus,'client_max_vus':args.max_vus,
                'tenant_count':args.tenants},stream,indent=2);stream.write('\n')
        elif args.action=='plan':
            jobs=formal_jobs(read_json(args.frozen));atomic_json(output,{'jobs':jobs,'protocol_seconds':sum(j['warmup_seconds']+j['measure_seconds'] for j in jobs)})
        elif args.action=='freeze':
            policy=read_json(args.policy);review=read_json(args.resource_review);probes=read_json(args.probes)
            if any(review.get(key) is not True for key in ('resource_stable','client_not_limited','no_sustained_wait_growth','reviewed_before_formal_matrix')):
                raise LoadError('Capacity freeze requires explicit resource/client/wait review')
            if probes.get('kind')!='probe' or probes.get('protocol_complete') is not True or probes.get('policy_sha256')!=hashlib.sha256(pathlib.Path(args.policy).read_bytes()).hexdigest():
                raise LoadError('Probe evidence must use the policy agreed before running probes')
            if args.environment and read_json(args.environment)!=probes['environment']:raise LoadError('Frozen environment differs from probes')
            frozen=freeze(output,probes['reports'],review,args.low,args.rated,args.overload,probes['environment'],policy['regression_tolerance'])
            atomic_bytes(ops.root/'.local/metrics/capacity.prom',
                ('# TYPE contextfence_capacity_p99_threshold_seconds gauge\ncontextfence_capacity_p99_threshold_seconds '+
                 str(frozen['latency_limits_ms']['p99']/1000)+'\n').encode())
        else:
            if os.name!='posix':raise LoadError('Real load execution requires the fixed Linux client environment')
            target=output.resolve();allowed=(ops.root/'artifacts/local').resolve()
            if not target.is_relative_to(allowed):raise LoadError('Runtime evidence must stay in ignored artifacts/local')
            policy=read_json(args.policy);policy_hash=hashlib.sha256(pathlib.Path(args.policy).read_bytes()).hexdigest()
            if (args.vus,args.max_vus,args.tenants)!=(policy['client_vus'],policy['client_max_vus'],policy['tenant_count']):
                raise LoadError('Client allocation and tenants must match the pre-agreed policy')
            env=environment(ops,args.k6,args.vus,args.max_vus);env['policy_sha256']=policy_hash
            if args.action in ('matrix','compare'):
                frozen=read_json(args.frozen)
                if args.action=='compare':verify_comparison(frozen['environment'],env,read_json(args.change))
                elif frozen['environment']!=env:raise LoadError('Formal environment differs from frozen environment')
                jobs=formal_jobs(frozen)
                if args.action=='compare':jobs=[job for job in jobs if job['scenario']!='cold' and job['step']=='rated']
            elif args.action=='probe':
                jobs=[{'kind':'probe','scenario':s,'rps':r,'warmup_seconds':30,'measure_seconds':60,'repeat':1}
                    for s in ('hot','single','multi') for r in (10,25,50,100,200)]
            else:
                if not 1<=args.seconds<=60:raise LoadError('Development probe must be between 1 and 60 seconds')
                jobs=[{'kind':'shortprobe','scenario':args.scenario,'rps':args.rps,'warmup_seconds':0,'measure_seconds':args.seconds,'repeat':1}]
            if args.resume:
                if args.action not in ('matrix','compare'):raise LoadError('Only formal runs support resume')
                document=read_json(output/'index.json')
                if document.get('environment')!=env or document.get('kind')!=args.action or document.get('policy_sha256')!=policy_hash:
                    raise LoadError('Resume environment or protocol differs from previous runs')
                jobs=remaining_jobs(jobs,document['reports'])
            else:
                output.mkdir(parents=True,exist_ok=False)
                document={'format_version':1,'kind':args.action,'environment':env,'policy_sha256':policy_hash,'reports':[],
                    'protocol_complete':False,'started_at':timestamp(time.time())}
            if args.action=='compare':document['single_variable_change']=read_json(args.change)
            atomic_json(output/'index.json',document)
            for index,job in enumerate(jobs,start=len(document['reports'])):
                print('Running '+job['kind']+' '+job['scenario']+' '+str(job['rps'])+' RPS repeat '+str(job['repeat']),flush=True)
                job_path=output/(str(index+1).zfill(2)+'-'+job['scenario']+'-'+str(job['rps']))
                if job_path.exists():job_path=job_path.with_name(job_path.name+'-retry-'+str(int(time.time())))
                report=run_job(ops,job,job_path,args.k6,args.vus,args.max_vus,args.tenants)
                report['evidence_directory']=job_path.name
                if args.action in ('matrix','compare'):
                    report['latency_regression_passed']=all(report['latency_ms'][name] is not None and report['latency_ms'][name]<=frozen['latency_limits_ms'][name] for name in ('p95','p99'))
                document['reports'].append(report);atomic_json(output/'index.json',document)
            document['protocol_complete']=True;document['ended_at']=timestamp(time.time());atomic_json(output/'index.json',document)
        print('PASS: load command completed; capacity claims require complete raw evidence and resource review')
        return 0
    except (LoadError,EvidenceError,OpsError,OSError,ValueError,KeyError,TypeError):
        print('FAIL: load command or evidence validation; private outputs were not printed')
        return 1


if __name__=='__main__':raise SystemExit(main())
