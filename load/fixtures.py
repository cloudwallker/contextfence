"""Versioned synthetic fixture feeder; all service traffic goes through HAProxy."""
import copy
import datetime as dt
import json
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import http.server
import secrets


class LoadError(ValueError):
    pass


def epoch(value):
    return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def timestamp(seconds):
    return dt.datetime.fromtimestamp(seconds,dt.timezone.utc).isoformat()


def validate_ingress(base,expected_port=58090):
    parsed=urllib.parse.urlparse(base)
    if expected_port not in (58090,58095) or parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.port != expected_port or parsed.path not in ('','/') or parsed.username or parsed.query or parsed.fragment:
        raise LoadError('Benchmark requires the configured loopback HAProxy ingress')
    return base.rstrip('/')


def load_identities(path):
    try:
        def unique(pairs):
            result={}
            for k,v in pairs:
                if k in result: raise LoadError('Duplicate identity key')
                result[k]=v
            return result
        document=json.loads(pathlib.Path(path).read_text(encoding='utf-8-sig'),object_pairs_hook=unique)
        tokens={}; tenants={}
        for principal in document['principals']:
            tenant=principal['tenant']; subject=principal['subject']; roles=set(principal['roles'])
            key=(tenant,subject)
            if key in tokens or not isinstance(principal['token'],str) or len(principal['token'])<32:
                raise LoadError('Invalid benchmark identity file')
            tokens[key]=principal['token']
            entry=tenants.setdefault(tenant,{'tenant':tenant})
            if {'READER','PRODUCER'} <= roles: entry.setdefault('reader',subject)
            if 'SOURCE_WRITER' in roles: entry.setdefault('writer',subject)
        complete=[entry for _,entry in sorted(tenants.items()) if 'reader' in entry and 'writer' in entry]
        if not complete: raise LoadError('Benchmark needs a complete synthetic reader/producer and writer tenant')
        return tokens,complete
    except (OSError,ValueError,KeyError,TypeError):
        raise LoadError('Benchmark identities unavailable or incomplete') from None


class IngressClient:
    def __init__(self,base,tokens,evidence=None,port=58090):
        self.base=validate_ingress(base,port); self.tokens=tokens; self.evidence=evidence
        self._log_lock=threading.Lock()

    def post(self,tenant,subject,path,payload):
        token=self.tokens.get((tenant,subject))
        if not token: raise LoadError('Fixture identity unavailable')
        request=urllib.request.Request(self.base+path,data=json.dumps(payload).encode(),method='POST',
            headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
        begin=time.perf_counter(); status=0; body=None
        try:
            try: response=urllib.request.urlopen(request,timeout=30)
            except urllib.error.HTTPError as failure: response=failure
            with response:
                status=response.status; body=json.load(response)
        except (OSError,ValueError):
            raise LoadError('Fixture ingress request failed') from None
        finally:
            if self.evidence:
                with self._log_lock:
                    with pathlib.Path(self.evidence).open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps({'recorded_at':timestamp(time.time()),'route':path,'status':status,
                            'e2e_ms':(time.perf_counter()-begin)*1000,'traffic':'feeder','tenant':tenant})+'\n')
        return {'status':status,'body':body}


class Feeder:
    def __init__(self,client,tenants,sources_per_tenant=8,run_id=None,body_bytes=1024):
        if not tenants or not 1<=sources_per_tenant<=128 or not 64<=body_bytes<=65536:
            raise LoadError('Invalid synthetic dataset configuration')
        self.client=client; self.tenants=copy.deepcopy(tenants); self.count=sources_per_tenant
        self.run_id=run_id or uuid.uuid4().hex[:16]; self.body_bytes=body_bytes
        self.lock=threading.RLock(); self.state={}; self.document=None
        self.next_refresh=0; self.next_cohort=0; self.cohort=0; self.reserved_writes=0
        for tenant in self.tenants:
            for index in range(self.count):
                marker='SYNTHETIC-BENCH-'+self.run_id+'-'+str(index)+'-'
                content=(marker+'x'*body_bytes)[:body_bytes]
                self.state[(tenant['tenant'],index)]={'source_id':'bench.'+self.run_id+'.'+str(index),
                    'sequence':0,'content':content,'readers':[tenant['reader']],'state':'ACTIVE','fresh_until':None}

    def _reserve(self,tenant,index,now):
        with self.lock:
            event=self.state[(tenant,index)]
            event['sequence']+=1; event['fresh_until']=timestamp(now+240)
            return copy.deepcopy(event)

    def reserve_write(self,tenant,index,now=None):
        try:
            event=self._reserve(tenant,index,time.time() if now is None else now)
        except KeyError:
            raise LoadError('Unknown synthetic write source') from None
        with self.lock: self.reserved_writes+=1
        return {'event':event,'expected':{'operation':'write','status':200,'source_id':event['source_id'],
                'sequence':event['sequence'],'content_version':1,'auth_epoch':1}}

    def tick(self,now=None):
        now=time.time() if now is None else now
        if now >= self.next_refresh:
            for tenant in self.tenants:
                for index in range(self.count):
                    event=self._reserve(tenant['tenant'],index,now)
                    response=self.client.post(tenant['tenant'],tenant['writer'],'/v1/source-events',event)
                    body=response['body']
                    if response['status']!=200 or body.get('sequence')!=event['sequence'] or body.get('source_id')!=event['source_id'] or \
                       body.get('outcome') not in ('APPLIED','IGNORED_STALE') or body.get('content_version')!=1 or body.get('auth_epoch')!=1:
                        raise LoadError('Fixture renewal changed source versions or was rejected')
            self.next_refresh=now+60
        if now >= self.next_cohort:
            # Publish only after EVERY source + derived handle is registered. The old TTL has 600s margin.
            tenant_rows=[]
            for tenant in self.tenants:
                rows=[]
                for index in range(self.count):
                    with self.lock: event=copy.deepcopy(self.state[(tenant['tenant'],index)])
                    root=self.client.post(tenant['tenant'],tenant['reader'],'/v1/contexts/source',
                        {'source_id':event['source_id'],'ttl_seconds':900})
                    if root['status']!=201: raise LoadError('SOURCE fixture registration rejected')
                    source=root['body']; derived_content=('SYNTHETIC-DERIVED-'+self.run_id+'-'+str(index)+'-'+'d'*self.body_bytes)[:self.body_bytes]
                    child=self.client.post(tenant['tenant'],tenant['reader'],'/v1/contexts/derived',
                        {'content':derived_content,'parent_ids':[source['id']],'ttl_seconds':900})
                    if child['status']!=201: raise LoadError('DERIVED fixture registration rejected')
                    derived=child['body']; versions=[{'source_id':event['source_id'],'content_version':1,'auth_epoch':1}]
                    if source.get('sources') != versions or derived.get('sources') != versions:
                        raise LoadError('Fixture handles have unexpected version bindings')
                    ids=[source['id'],derived['id']]
                    expected={'status':200,'code':'ALLOWED','context_ids':ids,'source_versions':versions,
                        'items':[{'id':source['id'],'content':event['content']},{'id':derived['id'],'content':derived_content}]}
                    rows.append({'index':index,'event':event,'context_ids':ids,'expected':expected})
                tenant_rows.append(dict(tenant,sources=rows))
            with self.lock:
                self.cohort+=1
                self.document={'format_version':1,'dataset_version':'synthetic-v1','cohort':self.cohort,
                    'published_at':timestamp(now),'body_bytes':self.body_bytes,'derived_depth':1,'assemble_batch_size':2,
                    'source_ttl_seconds':900,'refresh_seconds':60,'cohort_seconds':300,'tenants':tenant_rows}
            self.next_cohort=now+300
        else:
            with self.lock:
                for tenant in self.document['tenants']:
                    for source in tenant['sources']:
                        source['event']=copy.deepcopy(self.state[(tenant['tenant'],source['index'])])

    def snapshot(self):
        with self.lock:
            if self.document is None: raise LoadError('Fixture snapshot is not ready')
            return copy.deepcopy(self.document)


class FixtureServer:
    """Loopback-only transient snapshots; capability and tokens never enter evidence files."""
    def __init__(self,feeder,tokens,port=0):
        self.feeder=feeder;self.tokens=tokens;self.capability=secrets.token_hex(32)
        self.failure=None;self.aux_requests=0;self._lock=threading.Lock()
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,status,body):
                data=json.dumps(body).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def handle_call(self):
                if not secrets.compare_digest(self.headers.get('X-Fixture-Capability',''),owner.capability):
                    return self.reply(403,{'code':'FIXTURE_FORBIDDEN'})
                if owner.failure is not None:return self.reply(503,{'code':'FEEDER_FAILED'})
                try:
                    with owner._lock:owner.aux_requests+=1
                    if self.command=='GET' and self.path=='/snapshot':
                        document=owner.feeder.snapshot()
                        for tenant in document['tenants']:
                            tenant['reader_token']=owner.tokens[tenant['tenant'],tenant['reader']]
                            tenant['writer_token']=owner.tokens[tenant['tenant'],tenant['writer']]
                        return self.reply(200,document)
                    if self.command=='POST' and self.path=='/reserve':
                        length=int(self.headers.get('Content-Length','0'))
                        if not 1<=length<=1024:raise LoadError('Invalid reservation size')
                        payload=json.loads(self.rfile.read(length))
                        if set(payload)!={'tenant','index'} or type(payload['index']) is not int:raise LoadError('Invalid reservation')
                        return self.reply(200,owner.feeder.reserve_write(payload['tenant'],payload['index']))
                    return self.reply(404,{'code':'FIXTURE_NOT_FOUND'})
                except (LoadError,ValueError,KeyError,TypeError):return self.reply(400,{'code':'FIXTURE_INVALID'})
            do_GET=handle_call
            do_POST=handle_call
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler)
        self.server.daemon_threads=True
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)

    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
