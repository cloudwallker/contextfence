#!/usr/bin/env python3
"""仅在两个隔离 Compose 项目演练告警；原始正文、凭据与本地路径不进入报告。"""
import argparse
import concurrent.futures
import csv
import datetime as dt
import io
import json
import math
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PYTHON_IMAGE = 'public.ecr.aws/docker/library/python@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844'
LABELS = {'alertname','instance','job','severity','service','name','pool'}
POOL_ALERTS = ('ServerFailureRate','DatabasePoolWaiting','DatabasePoolTimeout','TailLatencyRegression')
WORKER_CODES = {'ALLOWED','DATABASE_UNAVAILABLE','SOURCE_ACCESS_DENIED','SOURCE_EXPIRED',
                'CONTEXT_STALE','CONTEXT_EXPIRED','CONTEXT_NOT_FOUND','INVALID_REQUEST','OTHER','TRANSPORT_FAILURE'}


class ProbeError(Exception):
    """错误消息仅使用固定描述，不携带捕获输出或本地路径。"""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def termination_interrupt(signum, frame):
    raise KeyboardInterrupt()


def approved_endpoints(configuration, action):
    expected = ('contextfence-bench',58095,59095) if action == 'pool' else ('contextfence-recovery',58096,59096)
    try:
        actual = (configuration['OPS_PROJECT_NAME'],int(configuration['ENTRY_PORT']),int(configuration['PROMETHEUS_PORT']))
    except (KeyError,TypeError,ValueError): raise ProbeError('隔离项目或端口配置缺失') from None
    if action not in ('metrics','disk','pool') or actual != expected:
        raise ProbeError('演练只允许对应隔离项目及匹配回环端口')
    return actual


def scoped_local(root, path):
    root = pathlib.Path(root).resolve(); local = root / '.local'; resolved = pathlib.Path(path).resolve()
    if local.resolve() != local or resolved == local or not resolved.is_relative_to(local):
        raise ProbeError('演练路径必须位于本项目真实 .local 目录内')
    return resolved


def require_container(document, project, service, network, database_host, published_port=None):
    labels = document.get('Config',{}).get('Labels',{})
    if labels.get('com.docker.compose.project') != project or labels.get('com.docker.compose.service') != service:
        raise ProbeError('容器项目或服务身份不匹配')
    if set(document.get('NetworkSettings',{}).get('Networks',{})) != {network}:
        raise ProbeError('容器必须仅连接本项目 backend 网络')
    bindings = document.get('HostConfig',{}).get('PortBindings') or {}
    actual_bindings={port:rows for port,rows in document.get('NetworkSettings',{}).get('Ports',{}).items() if rows}
    if published_port is None:
        if bindings or actual_bindings: raise ProbeError('应用、数据库及内部采集端不得开放宿主端口')
    else:
        internal = '8080/tcp' if service == 'proxy' else '9090/tcp'
        expected_binding={internal:[{'HostIp':'127.0.0.1','HostPort':str(published_port)}]}
        if bindings != expected_binding or actual_bindings!=expected_binding:
            raise ProbeError('实际入口或 Prometheus 回环绑定不匹配')
    if service in ('api-a','api-b'):
        values = [v.split('=',1)[1] for v in document.get('Config',{}).get('Env',[])
                  if v.startswith('CONTEXTFENCE_DATABASE_URL=')]
        if len(values) != 1 or not re.fullmatch(r'jdbc:postgresql://' + re.escape(database_host) + r':5432/[A-Za-z][A-Za-z0-9_]{0,62}',values[0]):
            raise ProbeError('应用实际数据库路由不匹配当前服务')


def atomic_restore(path, data, mode):
    from ops_common import atomic_bytes
    atomic_bytes(path,data,mode=mode)


class PreservedFiles:
    """任何注入退出均恢复原始字节、权限及不存在状态；逐文件报告清理失败。"""
    def __init__(self, paths): self.paths = [pathlib.Path(p) for p in paths]; self.saved = {}
    def __enter__(self):
        for path in self.paths:
            if path.is_symlink(): raise ProbeError('不允许替换符号链接指标文件')
            self.saved[path] = (path.read_bytes(),stat.S_IMODE(path.stat().st_mode)) if path.exists() else (None,None)
        return self
    def __exit__(self, *args):
        failed = False
        for path,(data,mode) in self.saved.items():
            try:
                if path.is_symlink(): raise ProbeError('恢复目标变为符号链接')
                if data is None: path.unlink(missing_ok=True)
                else: atomic_restore(path,data,mode)
            except BaseException: failed = True
        if failed: raise ProbeError('部分原始指标文件恢复失败；必须检查保留证据') from None


def expire_backup(raw, timestamp):
    pattern = rb'(?m)^(contextfence_backup_snapshot_timestamp_seconds[ \t]+)[^\r\n]+(?=\r?$)'
    if len(re.findall(pattern,raw)) != 1: raise ProbeError('备份指标必须具有唯一快照时间戳')
    return re.sub(pattern,lambda match:match[1]+str(timestamp).encode(),raw)


def safe_labels(labels):
    return {key:value for key,value in labels.items() if key in LABELS and isinstance(value,str)
            and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',value)}


def utc_seconds(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z',value):
        raise ProbeError('告警身份时间戳必须为严格 UTC')
    normalized=re.sub(r'\.([0-9]{1,9})(?=Z$)',lambda match:'.'+match[1][:6].ljust(6,'0'),value)
    try:return dt.datetime.fromisoformat(normalized[:-1]+'+00:00').timestamp()
    except ValueError:raise ProbeError('告警身份时间戳无效') from None


def delivered_alert(path, name, status, since, identity=None):
    begin = dt.datetime.fromisoformat(since.replace('Z','+00:00')).timestamp(); matches=[]
    # 与实际收集端轮转范围一致；不写入或模拟 webhook。
    paths = [pathlib.Path(str(path)+'.'+str(number)) for number in (3,2,1)] + [pathlib.Path(path)]
    for candidate in paths:
        if not candidate.exists(): continue
        with candidate.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    record=json.loads(line); recorded=dt.datetime.fromisoformat(record['recorded_at']).timestamp()
                    if recorded < begin or recorded > time.time()+5: continue
                    for alert in record.get('alerts',[]):
                        labels=alert.get('labels',{})
                        if identity is not None and status=='resolved':
                            try:
                                ended=utc_seconds(alert.get('endsAt'))
                                if ended<begin or ended>time.time()+5:continue
                            except ProbeError:continue
                        if labels.get('alertname') == name and alert.get('status') == status and \
                           (identity is None or all(alert.get(key)==value for key,value in identity.items())):
                            matches.append({'recorded_at':record['recorded_at'],'status':status,'labels':safe_labels(labels),
                                            **({'identity':identity} if identity is not None else {})})
                except (ValueError,KeyError,TypeError): raise ProbeError('实际告警收集记录损坏') from None
    return matches[-1] if matches else None


def verify_disk_binding(target, image, device, mount, loop):
    if not re.fullmatch(r'/dev/loop[0-9]+',device or ''): raise ProbeError('仅允许演练专属 loop 设备')
    rows=mount.get('filesystems',[]);loops=loop.get('loopdevices',[])
    if len(rows)!=1 or pathlib.Path(rows[0].get('target','')).resolve()!=pathlib.Path(target).resolve() or \
       rows[0].get('source')!=device or rows[0].get('fstype')!='ext4':
        raise ProbeError('ext4 挂载目标与 loop 设备不匹配')
    if len(loops)!=1 or loops[0].get('name')!=device or \
       pathlib.Path(loops[0].get('back-file','')).resolve()!=pathlib.Path(image).resolve():
        raise ProbeError('loop 设备未绑定演练镜像文件')


def calculate_fill_bytes(size, available):
    """按真实 ext4 有效容量保留 15%，至少 4 MiB，最多写入 104 MiB。"""
    if type(size) is not int or type(available) is not int or not 100*1024*1024<=size<=128*1024*1024 or not 0<=available<=size:
        raise ProbeError('loop 文件系统实际容量或可用空间无效')
    remaining=max(4*1024*1024,(size*15+99)//100)
    amount=min(104*1024*1024,available-remaining)
    if amount<=0 or (available-amount)/size>=.20:
        raise ProbeError('受限填充无法保留安全余量并触发既有告警条件')
    return amount


class DiskLoop:
    """仅 128 MiB loopfile、最多 104 MiB 填充；正常 umount，保留镜像证据。"""
    def __init__(self,ops,directory):
        self.ops=ops;self.directory=scoped_local(ops.root,directory)
        self.image=self.directory/'disk.img';self.target=self.directory/'mount';self.fill=self.target/'fill.bin'
        self.device=None;self.mounted=False;self.mount_attempted=False;self.loop_attempted=False
    def create(self):
        if os.name!='posix' or os.geteuid()!=0: raise ProbeError('磁盘演练需 Linux caller root 进行专属 loop 挂载')
        host=os.statvfs(self.ops.root/'.local')
        if host.f_bavail*host.f_frsize<384*1024*1024:
            raise ProbeError('loop 镜像承载文件系统至少需要 384 MiB 可用空间')
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=False);self.target.mkdir(mode=0o700)
        with self.image.open('xb') as stream: stream.truncate(128*1024*1024)
        if not stat.S_ISREG(self.image.stat().st_mode) or self.image.is_symlink() or self.image.stat().st_size != 128*1024*1024:
            raise ProbeError('loop 镜像安全检查失败')
        self.ops.run(['mkfs.ext4','-F','-m','0',str(self.image)],timeout=30)
        self.loop_attempted=True
        device=self.ops.run(['losetup','--find','--show','--nooverlap',str(self.image)],timeout=10).stdout.strip()
        if not re.fullmatch(r'/dev/loop[0-9]+',device): raise ProbeError('loop 分配返回非法设备')
        self.device=device
        self.verify_loop()
        self.mount_attempted=True
        self.ops.run(['mount','-t','ext4','-o','nosuid,nodev,noexec',device,str(self.target)],timeout=10)
        self.mounted=True;self.verify()
    def verify_loop(self):
        scoped_local(self.ops.root,self.image);scoped_local(self.ops.root,self.target)
        if self.image.is_symlink() or self.image.stat().st_size!=128*1024*1024 or not re.fullmatch(r'/dev/loop[0-9]+',self.device or ''):
            raise ProbeError('loop 镜像或设备范围发生变化')
        rows=json.loads(self.ops.run(['losetup','--json','--list',self.device,'--output','NAME,BACK-FILE'],timeout=10).stdout)
        devices=rows.get('loopdevices',[])
        if len(devices)!=1 or devices[0].get('name')!=self.device or pathlib.Path(devices[0].get('back-file','')).resolve()!=self.image:
            raise ProbeError('loop 镜像绑定发生变化')
        return rows
    def verify(self):
        loops=self.verify_loop()
        mounted=json.loads(self.ops.run(['findmnt','--json','--mountpoint',str(self.target),'--output','TARGET,SOURCE,FSTYPE'],timeout=10).stdout)
        verify_disk_binding(self.target,self.image,self.device,mounted,loops)
    def fill_disk(self):
        self.verify();block=os.stat(self.device)
        if not stat.S_ISBLK(block.st_mode):raise ProbeError('loop 目标不是块设备')
        expected=block.st_rdev
        directory=os.open(self.target,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            if os.fstat(directory).st_dev!=expected:raise ProbeError('目录已脱离已验证 loop 文件系统；禁止写入宿主盘')
            usage=os.fstatvfs(directory)
            amount=calculate_fill_bytes(usage.f_blocks*usage.f_frsize,usage.f_bavail*usage.f_frsize)
            descriptor=os.open('fill.bin',os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=directory)
            with os.fdopen(descriptor,'wb') as stream:
                if os.fstat(stream.fileno()).st_dev!=expected:raise ProbeError('填充文件不属于已验证 loop 文件系统')
                chunk=b'\x00'*(1024*1024)
                left=amount
                while left:
                    block=chunk[:min(left,len(chunk))]
                    if stream.write(block)!=len(block):raise ProbeError('受限填充写入不完整')
                    left-=len(block)
                stream.flush();os.fsync(stream.fileno())
                filled=os.fstat(stream.fileno()).st_size
                if filled!=amount:raise ProbeError('受限填充实际字节数不匹配')
            self.verify();usage=os.fstatvfs(directory)
            size=usage.f_blocks*usage.f_frsize;available=usage.f_bavail*usage.f_frsize
            if not 100*1024*1024<=size<=128*1024*1024 or available<4*1024*1024 or available/size>=.20:
                raise ProbeError('受限 loop 填充未实际达到安全余量及既有告警条件')
            return {'filesystem_bytes':size,'filled_bytes':filled,'available_fraction':round(available/size,6)}
        finally:os.close(directory)
    def clear_fill(self):
        if self.mounted:
            self.verify()
            if self.fill.is_symlink(): raise ProbeError('填充文件发生不安全变化')
            self.fill.unlink(missing_ok=True)
            descriptor=os.open(self.target,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
    def cleanup(self):
        if self.loop_attempted and self.device is None:
            result=self.ops.run(['losetup','--json','--list','--associated',str(self.image),'--output','NAME,BACK-FILE'],timeout=10)
            rows=json.loads(result.stdout).get('loopdevices',[])
            if len(rows)>1 or any(pathlib.Path(row.get('back-file','')).resolve()!=self.image for row in rows):
                raise ProbeError('未知 loop 分配结果未能限定到本轮镜像')
            if rows:self.device=rows[0].get('name');self.verify_loop()
        if self.mount_attempted and not self.mounted:
            found=self.ops.run(['findmnt','--json','--mountpoint',str(self.target),'--output','TARGET,SOURCE,FSTYPE'],check=False,timeout=10)
            if found.returncode==0:
                verify_disk_binding(self.target,self.image,self.device,json.loads(found.stdout),self.verify_loop())
                self.mounted=True
            elif found.returncode!=1:raise ProbeError('不能确认异常挂载的实际状态')
        if self.mounted:
            self.clear_fill();self.verify()
            self.ops.run(['umount',str(self.target)],timeout=15);self.mounted=False
        if self.device:
            self.verify_loop();self.ops.run(['losetup','--detach',self.device],timeout=10);self.device=None


def frozen_threshold(document, gauge):
    try:
        value=document['latency_limits_ms']['p99'];baseline=document['baseline_latency_ms']['p99'];tolerance=document['regression_tolerance']
        if document['format_version']!=1 or document['resource_review']['resource_stable'] is not True or not document['environment']:
            raise ValueError()
        if any(type(number) not in (int,float) or not math.isfinite(number) for number in (value,baseline,tolerance)) or \
           baseline<=0 or not 0<=tolerance<=.5 or not math.isclose(value,baseline*(1+tolerance),rel_tol=1e-9):raise ValueError()
        matches=re.findall(rb'(?m)^contextfence_capacity_p99_threshold_seconds[ \t]+([^\r\n]+)$',gauge)
        if len(matches)!=1 or not math.isclose(float(matches[0]),value/1000,rel_tol=1e-9):raise ValueError()
        return value/1000
    except (KeyError,TypeError,ValueError,OverflowError): raise ProbeError('必须使用真实冻结并匹配已发布指标的 p99 门槛') from None


def sidecar_command(project,network,run_id,module,identities,fixture):
    if project!='contextfence-bench' or network!='contextfence-bench_backend' or not re.fullmatch(r'[0-9a-f]{6,32}',run_id):
        raise ProbeError('诊断侧车只允许专属 bench backend 网络')
    name=project+'-alert-probe-'+run_id
    args=['docker','run','--detach','--name',name,'--label','com.docker.compose.project='+project,
          '--label','com.docker.compose.service=alert-probe','--label','contextfence.alert-probe='+run_id,
          '--network',network,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
          '--user','65534:65534','--memory','128m','--cpus','1','--pids-limit','128',
          '--log-driver','json-file','--log-opt','max-size=1m','--log-opt','max-file=2']
    for source,destination in ((module,'/probe/alert_probe.py'),(identities,'/probe/identities.json'),(fixture,'/probe/fixture.json')):
        if ',' in str(source):raise ProbeError('诊断挂载路径不可包含分隔符')
        args+=['--mount','type=bind,src='+str(pathlib.Path(source).resolve())+',dst='+destination+',readonly']
    return args+['--entrypoint','python',PYTHON_IMAGE,'/probe/alert_probe.py','--worker','--seconds','240']


def worker_verdict(status, body):
    status=status if type(status) is int and 100<=status<=599 else 0
    code=body.get('code') if isinstance(body,dict) else 'OTHER'
    return {'status':status,'code':code if code in WORKER_CODES else 'OTHER'}


def run_worker(seconds=240):
    """自包含诊断流量：每实例 40 线程，固定路径只读私有身份，绝不输出正文。"""
    if seconds!=240: raise ProbeError('诊断侧车运行时间固定为 240 秒')
    identities=json.loads(pathlib.Path('/probe/identities.json').read_text())
    tokens=[row['token'] for row in identities['principals'] if (row['tenant'],row['subject'])==('acme','alice')]
    fixture=json.loads(pathlib.Path('/probe/fixture.json').read_text())
    if len(tokens)!=1 or not re.fullmatch(r'[A-Za-z0-9._~-]{24,256}',tokens[0]) or not re.fullmatch(r'[0-9a-f-]{36}',fixture['context_id']):
        raise ProbeError('诊断私有夹具或身份不完整')
    payload=json.dumps({'context_ids':[fixture['context_id']]}).encode();stop=threading.Event();mutex=threading.Lock()
    totals={node:{'requests':0,'status':{},'code':{}} for node in ('api-a','api-b')}
    def halted(*args):stop.set()
    signal.signal(signal.SIGTERM,halted);signal.signal(signal.SIGINT,halted)
    deadline=time.monotonic()+seconds
    def request_loop(node):
        while not stop.is_set() and time.monotonic()<deadline:
            verdict={'status':0,'code':'TRANSPORT_FAILURE'}
            request=urllib.request.Request('http://'+node+':8080/v1/contexts/assemble',payload,
                {'Authorization':'Bearer '+tokens[0],'Content-Type':'application/json'},method='POST')
            try:
                try:response=urllib.request.urlopen(request,timeout=40)
                except urllib.error.HTTPError as error:response=error
                with response:
                    raw=response.read(262145)
                    body=json.loads(raw) if len(raw)<=262144 else None
                    verdict=worker_verdict(response.status,body)
            except (OSError,ValueError,TypeError):pass
            with mutex:
                row=totals[node];row['requests']+=1
                for key in ('status','code'):
                    label=str(verdict[key]);row[key][label]=row[key].get(label,0)+1
            stop.wait(.01)
    def summary():
        with mutex:print(json.dumps({'format_version':1,'traffic_kind':'internal-diagnostic-only','counters':totals},sort_keys=True),flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=80) as executor:
        futures=[executor.submit(request_loop,node) for node in ('api-a','api-b') for _ in range(40)]
        while not stop.wait(5) and time.monotonic()<deadline:summary()
        stop.set()
        for future in futures:future.result()
    summary();return 0


class DatabaseLock:
    def __init__(self,ops,run_id):
        if not re.fullmatch(r'[0-9a-f]{6,32}',run_id):raise ProbeError('锁演练标识非法')
        self.ops=ops;self.application='cf-alertprobe-'+run_id;self.process=None;self.confirmed_at=None
    def start(self):
        self.process=subprocess.Popen(self.ops.database_args('runtime'),cwd=self.ops.root,
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True)
        sql="SET application_name='"+self.application+"'; BEGIN; SET LOCAL statement_timeout='215s'; " \
            "SELECT tenant FROM tenant_guard WHERE tenant='acme' FOR UPDATE; SELECT pg_sleep(210); ROLLBACK;\n"
        self.process.stdin.write(sql);self.process.stdin.flush();self.process.stdin.close()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if self.process.poll() is not None:raise ProbeError('受控写锁事务提前退出')
            value=self.ops.database('monitor',"SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND usename='cf_runtime' AND application_name='"+self.application+"' AND wait_event='PgSleep';").strip()
            if value=='1':self.confirmed_at=time.monotonic();return
            time.sleep(.25)
        raise ProbeError('受控写锁未获得数据库确认')
    def complete_hold(self,tick):
        deadline=self.confirmed_at+225
        while True:
            code=self.process.poll()
            if code is not None:
                if code!=0:raise ProbeError('受控 210 秒锁事务未正常完成')
                return {'requested_hold_seconds':210,'transaction_completed':True,
                        'observed_transaction_seconds':round(time.monotonic()-self.confirmed_at,3)}
            if time.monotonic()>=deadline:raise ProbeError('受控锁事务超出完成预算')
            tick();time.sleep(2)
    def release(self):
        if self.process is None:return
        # 终止 Docker exec 客户端本身无法证明 PostgreSQL 已释放事务，按唯一标签精确关闭连接。
        self.ops.database('admin',"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() "
            "AND usename='cf_runtime' AND application_name='"+self.application+"' AND pid<>pg_backend_pid();")
        try:self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate();self.process.wait(timeout=15)
        remaining=self.ops.database('monitor',"SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND usename='cf_runtime' AND application_name='"+self.application+"';").strip()
        if remaining!='0':raise ProbeError('受控写锁事务仍存在；恢复未通过')
        self.process=None


class PoolRecovery:
    def __init__(self,ops,lock,sidecar,ingress):self.ops=ops;self.lock=lock;self.sidecar=sidecar;self.ingress=ingress
    def __enter__(self):return self
    def __exit__(self,*args):
        failures=[]
        actions=[self.lock.release,self.sidecar.stop]
        for node in ('api-a','api-b'):
            actions.extend([lambda node=node:self.ops.wait_ready(node,timeout=120),lambda node=node:self.ops.smoke(node),lambda node=node:self.ops.resume(node)])
        actions.append(self.ingress)
        for action in actions:
            try:action()
            except BaseException:failures.append(True)
        if failures:
            try:self.ops.close_maintenance('alert-probe-recovery-failed')
            except BaseException:pass
            raise ProbeError('锁、侧车或双实例安全恢复失败；失败证据已保留') from None


class Sidecar:
    def __init__(self,ops,scope,run_id,fixture):
        self.ops=ops;self.scope=scope;self.run_id=run_id;self.fixture=fixture
        self.name=scope['project']+'-alert-probe-'+run_id;self.created=False
    def inspect(self):
        data=json.loads(self.ops.run(['docker','inspect',self.name],timeout=10).stdout)[0]
        require_container(data,self.scope['project'],'alert-probe',self.scope['network'],self.scope['database'])
        if data['Config']['Labels'].get('contextfence.alert-probe')!=self.run_id or data['HostConfig'].get('Privileged') or not data['HostConfig'].get('ReadonlyRootfs'):
            raise ProbeError('诊断侧车实际身份或权限不匹配')
        mounts={row.get('Destination'):row for row in data.get('Mounts',[])}
        expected={'/probe/alert_probe.py':pathlib.Path(__file__).resolve(),'/probe/identities.json':self.ops.root/'.local/identities.json','/probe/fixture.json':self.fixture}
        if set(mounts)!=set(expected) or any(mounts[dest].get('RW') is not False or pathlib.Path(mounts[dest].get('Source','')).resolve()!=path.resolve() for dest,path in expected.items()):
            raise ProbeError('诊断侧车私有挂载不匹配或可写')
        return data
    def start(self):
        self.created=True  # Docker 响应不确定时 finally 仍需核对并停止精确名称。
        args=sidecar_command(self.scope['project'],self.scope['network'],self.run_id,pathlib.Path(__file__).resolve(),self.ops.root/'.local/identities.json',self.fixture)
        self.ops.run(args,timeout=60);self.inspect()
    def sample(self):
        if not self.inspect().get('State',{}).get('Running'):raise ProbeError('诊断侧车提前停止')
        raw=self.ops.run(['docker','logs','--tail','1',self.name],timeout=10).stdout.strip()
        if not raw:return None
        row=json.loads(raw);counters=row.get('counters',{})
        if row.get('traffic_kind')!='internal-diagnostic-only' or set(counters)!={'api-a','api-b'}:
            raise ProbeError('诊断侧车计数器结构非法')
        for values in counters.values():
            if set(values)!={'requests','status','code'} or type(values['requests']) is not int or values['requests']<0:raise ProbeError('诊断计数器非法')
            if any(key not in WORKER_CODES for key in values['code']) or any(not re.fullmatch(r'(0|[1-5][0-9]{2})',key) for key in values['status']):raise ProbeError('诊断标签非法')
            if any(type(v) is not int or v<0 for group in ('status','code') for v in values[group].values()):raise ProbeError('诊断计数值非法')
        return {'traffic_kind':row['traffic_kind'],'counters':counters}
    def stop(self):
        if not self.created:return
        found=self.ops.run(['docker','inspect',self.name],check=False,timeout=10)
        if found.returncode:
            listed=self.ops.run(['docker','container','ls','--all','--filter','name=^/'+self.name+'$','--format','{{.ID}}'],check=False,timeout=10)
            if listed.returncode or listed.stdout.strip():raise ProbeError('不能确认诊断侧车不存在；恢复未通过')
            self.created=False;return
        self.inspect();self.ops.run(['docker','stop','--time','45',self.name],timeout=60)
        if self.inspect().get('State',{}).get('Running'):raise ProbeError('诊断侧车未停止')
        self.created=False


def shared_helpers(root):
    sys.path.insert(0,str(root));sys.path.insert(0,str(root/'scripts'))
    from ops_common import Ops, OpsError, atomic_json, gate_is_open
    return Ops,OpsError,atomic_json,gate_is_open


def verify_scope(ops,action):
    project,entry,prom=approved_endpoints(ops.env(),action);network=project+'_backend';database=ops.database_service()
    if ops.database_host()!=database or not re.fullmatch(r'(postgres|recovery-db-[0-9a-f]{6,32})',database):
        raise ProbeError('当前数据库必须为真实服务，不能是中断代理')
    network_data=json.loads(ops.run(['docker','network','inspect',network],timeout=10).stdout)[0]
    labels=network_data.get('Labels',{})
    if labels.get('com.docker.compose.project')!=project or labels.get('com.docker.compose.network')!='backend':raise ProbeError('backend 网络归属不匹配')
    for service in ('api-a','api-b',database,'proxy','prometheus','alertmanager','alert-receiver','node-exporter'):
        identifier=ops.compose('ps','--quiet',service).stdout.strip()
        if not re.fullmatch(r'[0-9a-f]{12,64}',identifier):raise ProbeError('演练目标未运行或身份不唯一')
        data=json.loads(ops.run(['docker','inspect',identifier],timeout=10).stdout)[0]
        require_container(data,project,service,network,database,entry if service=='proxy' else prom if service=='prometheus' else None)
        if not data.get('State',{}).get('Running'):raise ProbeError('演练目标未运行')
        destination='/textfile' if service=='node-exporter' else '/data' if service=='alert-receiver' else None
        if destination:
            source=ops.root/'.local'/('metrics' if service=='node-exporter' else 'alerts')
            scoped_local(ops.root,source)
            mounts=[m for m in data.get('Mounts',[]) if m.get('Destination')==destination]
            if len(mounts)!=1 or pathlib.Path(mounts[0]['Source']).resolve()!=source.resolve():raise ProbeError('采集端实际目录不属于此隔离项目')
    from ops_common import gate_is_open
    if not gate_is_open(ops.app_gate) or not gate_is_open(ops.proxy_gate):raise ProbeError('演练必须从已验收开放的隔离入口开始')
    return {'project':project,'entry':entry,'prometheus':prom,'network':network,'database':database}


class Probe:
    def __init__(self,ops,action,scope):
        self.ops=ops;self.action=action;self.scope=scope;self.run_id=uuid.uuid4().hex[:12]
        self.started=time.monotonic();self.rows=[];self.identities={};self.query='http://127.0.0.1:'+str(scope['prometheus'])
    def event(self,phase,**values):
        self.rows.append({'phase':phase,'recorded_at':utc_now(),'elapsed_seconds':round(time.monotonic()-self.started,3),**values})
    def prometheus(self,endpoint='/api/v1/alerts',query=None):
        url=self.query+endpoint+(('?'+urllib.parse.urlencode({'query':query})) if query else '')
        try:
            with urllib.request.urlopen(url,timeout=10) as reply:document=json.load(reply)
            if document.get('status')!='success':raise ValueError()
            return document['data']
        except (OSError,ValueError,KeyError):raise ProbeError('实际 Prometheus 查询失败') from None
    def alertmanager(self):
        code="import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://alertmanager:9093/api/v2/alerts',timeout=5))))"
        result=self.ops.compose('exec','-T','alert-receiver','python','-c',code,timeout=10)
        rows=json.loads(result.stdout)
        if not isinstance(rows,list):raise ProbeError('实际 Alertmanager 查询结构非法')
        return rows
    def require_baseline(self,names):
        if any(row.get('labels',{}).get('alertname') in names for row in self.prometheus().get('alerts',[])):
            raise ProbeError('目标告警已 pending 或 firing；先恢复正常基线再开始新演练')
        self.event('actual-target-alerts-clear-baseline',alerts=list(names))
    def wait_alerts(self,names,status,since,budget,predicate=None,tick=None,strict_clear=False):
        deadline=time.monotonic()+budget;pending=set(names);begin=dt.datetime.fromisoformat(since.replace('Z','+00:00')).timestamp()
        while pending and time.monotonic()<deadline:
            prom=self.prometheus().get('alerts',[]);manager=self.alertmanager()
            targets_clear=not strict_clear or (not any(row.get('labels',{}).get('alertname') in names for row in prom)
                and not any(row.get('labels',{}).get('alertname') in names for row in manager))
            for name in tuple(pending):
                firing=[row for row in prom if row.get('labels',{}).get('alertname')==name and row.get('state')=='firing' and (predicate is None or predicate(row.get('labels',{})))]
                active=[row for row in manager if row.get('labels',{}).get('alertname')==name and row.get('status',{}).get('state')=='active' and (predicate is None or predicate(row.get('labels',{})))]
                delivered=None
                if status=='firing':
                    for candidate in active:
                        if not any(row.get('labels')==candidate.get('labels') for row in firing):continue
                        identity={'fingerprint':candidate.get('fingerprint'),'startsAt':candidate.get('startsAt')}
                        if not re.fullmatch(r'[0-9a-f]{16}',identity['fingerprint'] or '') or not isinstance(identity['startsAt'],str):
                            raise ProbeError('实际 Alertmanager 缺失可关联的告警身份')
                        started=utc_seconds(identity['startsAt'])
                        if started<begin or started>time.time()+5:continue
                        delivered=delivered_alert(self.ops.root/'.local/alerts/alerts.jsonl',name,status,since,identity)
                        if delivered:self.identities[name]=identity;break
                else:
                    if name not in self.identities:raise ProbeError('缺失本次触发身份，不能接受同名恢复通知')
                    delivered=delivered_alert(self.ops.root/'.local/alerts/alerts.jsonl',name,status,since,self.identities[name])
                if delivered and ((firing and active) if status=='firing' else not firing and not active and targets_clear):
                    self.event('actual-alert-'+status,alert=name,delivery=delivered,
                               prometheus_firing=bool(firing),alertmanager_active=bool(active));pending.remove(name)
            if tick:tick()
            if pending:time.sleep(2)
        if pending:raise ProbeError('实际告警触发或恢复未在预算内完整交付')
    def report(self,passed):
        return {'format_version':1,'action':self.action,'project':self.scope['project'],'run_id':self.run_id,
                'runtime_passed':passed,'duration_seconds':round(time.monotonic()-self.started,3),'observations':self.rows,
                'scope':'isolated synthetic fixture; real Prometheus, Alertmanager and local receiver'}


def metrics_probe(probe):
    from load.telemetry import LoadCounters
    directory=scoped_local(probe.ops.root,probe.ops.root/'.local/metrics')
    backup=directory/'backup.prom';prom=directory/'load.prom';state=directory/'load-counters.json'
    if not backup.is_file():raise ProbeError('备份演练需要已有真实成功备份指标')
    names=('BackupExpired','LoadUnexpectedFailureRate','LoadUnsafeAllow')
    probe.require_baseline(names)
    with PreservedFiles([backup,prom,state]):
        counters=LoadCounters(directory);counters.publish()
        probe.event('synthetic-counter-baseline-published',input_kind='controlled metric input; no service unsafe admission')
        # 两次抓取形成 rate/increase 的真实基线，不向收集端发送任何伪告警。
        time.sleep(12);started=utc_now()
        atomic_restore(backup,expire_backup(backup.read_bytes(),int(time.time())-7200),stat.S_IMODE(backup.stat().st_mode))
        for index in range(200):
            category='unsafe_allow' if index==0 else 'unexpected_failure' if index<10 else 'success'
            counters.add('hot',{'type':'Point','metric':'cf_e2e_ms','data':{'value':0,'tags':{'issued':'1','phase':'measure','operation':'read','category':category}}})
        counters.publish();probe.event('controlled-synthetic-metric-inputs',requests=200,unexpected=10,unsafe_input=1,
                                       service_unsafe_admission_observed=False)
        probe.wait_alerts(names,'firing',started,150)
        restored=utc_now()
    probe.event('original-metric-bytes-restored')
    probe.wait_alerts(names,'resolved',restored,210)


def disk_probe(probe):
    directory=scoped_local(probe.ops.root,probe.ops.root/'.local/alert-probes'/probe.run_id)
    disk=DiskLoop(probe.ops,directory);injected=False;failure=None
    try:
        disk.create()
        probe.ops.compose('up','--detach','--no-deps','--no-build','--force-recreate','node-exporter',timeout=120)
        # 路径与 device 仅用于内部归属断言，报告只输出容量。
        sample=probe.prometheus('/api/v1/query','node_filesystem_size_bytes{device="'+disk.device+'",fstype="ext4"}')
        deadline=time.monotonic()+30
        while not sample.get('result') and time.monotonic()<deadline:
            time.sleep(2);sample=probe.prometheus('/api/v1/query','node_filesystem_size_bytes{device="'+disk.device+'",fstype="ext4"}')
        if not sample.get('result') or not any(row.get('metric',{}).get('mountpoint')==str(disk.target) for row in sample['result']):
            raise ProbeError('采集端未发现演练专属 ext4 子挂载')
        started=utc_now();usage=disk.fill_disk();injected=True;probe.event('actual-ext4-bounded-fill',**usage)
        predicate=lambda labels:labels.get('device')==disk.device and labels.get('mountpoint')==str(disk.target)
        probe.wait_alerts(('HostDiskLow',),'firing',started,210,predicate)
    except BaseException as error:failure=error
    finally:
        cleanup_errors=[]
        if disk.mounted:
            try:
                restored=utc_now();disk.clear_fill();probe.event('verified-ext4-fill-removed')
                if injected:probe.wait_alerts(('HostDiskLow',),'resolved',restored,180,
                    lambda labels:labels.get('device')==disk.device and labels.get('mountpoint')==str(disk.target))
            except BaseException:cleanup_errors.append(True)
        try:
            if disk.mounted:probe.ops.compose('stop','node-exporter',timeout=30)
        except BaseException:cleanup_errors.append(True)
        try:
            disk.cleanup()
        except BaseException:cleanup_errors.append(True)
        finally:
            try:probe.ops.compose('up','--detach','--no-deps','--no-build','node-exporter',timeout=120)
            except BaseException:cleanup_errors.append(True)
        if cleanup_errors:raise ProbeError('磁盘或采集端恢复失败；保留 loop 镜像与失败证据') from None
    if failure:raise failure
    probe.event('loop-detached-image-retained',image_bytes=128*1024*1024)


def seed_pool_fixture(ops,directory):
    from fixture_producer import record_event,fresh_deadline,expect
    directory.mkdir(mode=0o755,parents=True,exist_ok=False)
    source='alert-probe.'+uuid.uuid4().hex[:12];ledger=directory/'ledger.json';fixture=directory/'fixture.json'
    event={'source_id':source,'sequence':1,'content':'SYNTHETIC-ALERT-PROBE','readers':['alice'],'state':'ACTIVE','fresh_until':fresh_deadline()}
    record_event(ops,event,ledger_path=ledger)
    handle=expect(ops,'api-a','/v1/contexts/source',{'source_id':source,'ttl_seconds':300},status=201)
    for node in ('api-a','api-b'):
        expect(ops,node,'/v1/contexts/assemble',{'context_ids':[handle['id']]},code='ALLOWED')
    atomic_restore(fixture,(json.dumps({'format_version':1,'context_id':handle['id']})+'\n').encode(),0o644)
    return fixture


def wait_ingress(ops,timeout=60):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        rows=list(csv.DictReader(io.StringIO(ops.proxy_command('show stat').removeprefix('# '))))
        if all(any(row.get('pxname')=='contextfence' and row.get('svname')==node and row.get('status')=='UP' for row in rows) for node in ('api-a','api-b')):return
        time.sleep(.5)
    raise ProbeError('双后端未恢复统一入口健康状态')


def pool_probe(probe,frozen):
    threshold=frozen_threshold(json.loads(pathlib.Path(frozen).read_text()),(probe.ops.root/'.local/metrics/capacity.prom').read_bytes())
    probe.event('actual-frozen-threshold-verified',threshold_seconds=threshold)
    probe.require_baseline(POOL_ALERTS)
    directory=scoped_local(probe.ops.root,probe.ops.root/'.local/alert-probes'/probe.run_id)
    fixture=seed_pool_fixture(probe.ops,directory);lock=DatabaseLock(probe.ops,probe.run_id)
    sidecar=Sidecar(probe.ops,probe.scope,probe.run_id,fixture)
    def sample():
        sql="SELECT json_build_object('holders',count(*) FILTER(WHERE application_name='"+lock.application+"'), " \
            "'lock_waiters',count(*) FILTER(WHERE wait_event_type='Lock'), " \
            "'blocked_sessions',count(*) FILTER(WHERE cardinality(pg_blocking_pids(pid))>0)) FROM pg_stat_activity " \
            "WHERE datname=current_database() AND usename='cf_runtime';"
        data=json.loads(probe.ops.database('monitor',sql))
        if set(data)!={'holders','lock_waiters','blocked_sessions'} or any(type(v) is not int or not 0<=v<=80 for v in data.values()):raise ProbeError('PG 锁诊断结果非法')
        probe.event('actual-pg-lock-sample',**data)
        worker=sidecar.sample()
        if worker:probe.event('internal-diagnostic-traffic',**worker)
        for metric in ('hikaricp_connections_pending','hikaricp_connections_timeout_total'):
            rows=probe.prometheus('/api/v1/query',metric+'{job="contextfence"}').get('result',[])
            values=[]
            for row in rows:
                value=float(row['value'][1]);instance=row.get('metric',{}).get('instance')
                if instance in ('api-a','api-b') and math.isfinite(value):values.append({'instance':instance,'value':value})
            probe.event('actual-pool-metric',metric=metric,samples=values)
    with PoolRecovery(probe.ops,lock,sidecar,lambda:wait_ingress(probe.ops)):
        started=utc_now();lock.start();sidecar.start()
        probe.event('actual-tenant-guard-write-lock',requested_hold_seconds=210,threads_per_instance=40,
                    traffic_kind='internal-diagnostic-only; not HAProxy capacity evidence')
        probe.wait_alerts(POOL_ALERTS,'firing',started,195,tick=sample)
        probe.event('real-lock-transaction-completed',**lock.complete_hold(sample))
    probe.event('lock-released-sidecar-stopped-dual-smoke-ingress-recovered')
    # 某条告警可能在其余告警触发前恢复；清理后仍须两个监控平面所有目标均已清空。
    probe.wait_alerts(POOL_ALERTS,'resolved',started,240,strict_clear=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',choices=('metrics','disk','pool'))
    parser.add_argument('--root');parser.add_argument('--output');parser.add_argument('--frozen')
    parser.add_argument('--worker',action='store_true');parser.add_argument('--seconds',type=int,default=240)
    args=parser.parse_args(argv)
    if args.worker:
        try:return run_worker(args.seconds)
        except Exception:
            print(json.dumps({'worker_failed':True,'code':'PRIVATE_FIXTURE_OR_RUNTIME_FAILURE'}));return 1
    if args.action is None or args.root is None or args.output is None:parser.error('演练需 action、--root 与新的 --output')
    root=pathlib.Path(args.root).resolve();probe=None;lock_file=None;output=None;owned_lock=False;original_signal=None
    Ops,OpsError,atomic_json,gate_is_open=shared_helpers(root)
    try:
        if os.name!='posix':raise ProbeError('实际告警演练仅在固定 Linux 环境运行')
        original_signal=signal.signal(signal.SIGTERM,termination_interrupt)
        output=scoped_local(root,args.output)
        if output.exists():raise ProbeError('证据路径已存在；不能覆盖历史记录')
        ops=Ops(root);scope=verify_scope(ops,args.action)
        lock_file=scoped_local(root,root/'.local/alert-probe.lock')
        descriptor=os.open(lock_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);owned_lock=True;os.close(descriptor)
        probe=Probe(ops,args.action,scope);probe.event('actual-isolated-scope-verified')
        if args.action=='metrics':metrics_probe(probe)
        elif args.action=='disk':disk_probe(probe)
        else:
            if not args.frozen:raise ProbeError('连接池演练必须指定真实冻结 JSON')
            pool_probe(probe,args.frozen)
        atomic_json(output,probe.report(True));print('PASS：隔离告警演练 '+args.action+'；私有输入已省略');return 0
    except (Exception,KeyboardInterrupt):
        if probe is not None and output is not None:
            probe.event('acceptance-failed-evidence-retained')
            try:atomic_json(output,probe.report(False))
            except Exception:pass
        print('FAIL：隔离告警演练未通过；恢复失败不会视为成功，检查私有证据');return 1
    finally:
        if original_signal is not None:signal.signal(signal.SIGTERM,original_signal)
        if owned_lock:lock_file.unlink(missing_ok=True)


if __name__=='__main__':raise SystemExit(main())
