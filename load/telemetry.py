"""Client-verdict counters for node-exporter; never infer expected failures from HTTP status."""
import json
import pathlib
import sys
import threading

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from ops_common import atomic_bytes,atomic_json
from load.analyze import EvidenceError

SCENARIOS=('cold','hot','single','multi')
CATEGORIES={'success','expected_denial','unexpected_failure','unsafe_allow','timeout','transport_failure','client_failure'}


class LoadCounters:
    def __init__(self,directory):
        self.directory=pathlib.Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.state=self.directory/'load-counters.json';self.lock=threading.RLock()
        self.totals={scenario:{'requests':0,'unexpected':0,'unsafe_allow':0} for scenario in SCENARIOS}
        if self.state.exists():
            document=json.loads(self.state.read_text(encoding='utf-8'))
            if set(document)!=set(SCENARIOS):raise EvidenceError('Invalid persisted client counters')
            for row in document.values():
                if set(row)!={'requests','unexpected','unsafe_allow'} or any(type(value) is not int or value<0 for value in row.values()) or \
                   not row['unsafe_allow']<=row['unexpected']<=row['requests']:
                    raise EvidenceError('Invalid persisted client counter values')
            self.totals=document
    def add(self,scenario,point):
        if scenario not in SCENARIOS:raise EvidenceError('Unbounded client scenario label')
        if point.get('type')!='Point' or point.get('metric')!='cf_e2e_ms':return
        tags=point.get('data',{}).get('tags',{})
        if tags.get('issued')!='1':return
        category=tags.get('category')
        if category not in CATEGORIES or category=='client_failure':raise EvidenceError('Invalid issued business verdict')
        if tags.get('operation') not in ('read','write') or tags.get('phase') not in ('warmup','measure'):
            raise EvidenceError('Incomplete original business verdict')
        with self.lock:
            row=self.totals[scenario];row['requests']+=1
            if category not in ('success','expected_denial'):row['unexpected']+=1
            if category=='unsafe_allow':row['unsafe_allow']+=1
    def publish(self):
        with self.lock:
            atomic_json(self.state,self.totals)
            lines=[]
            for metric,key in (('requests','requests'),('unexpected','unexpected'),('unsafe_allow','unsafe_allow')):
                name='contextfence_load_'+metric+'_total';lines.append('# TYPE '+name+' counter')
                for scenario in SCENARIOS:
                    lines.append(name+'{scenario="'+scenario+'"} '+str(self.totals[scenario][key]))
            atomic_bytes(self.directory/'load.prom',('\n'.join(lines)+'\n').encode())


class RawLoadExporter:
    def __init__(self,raw,scenario,counters):
        self.raw=pathlib.Path(raw);self.scenario=scenario;self.counters=counters
        self.position=0;self.pending=b'';self.stop=threading.Event();self.failure=False
        self._failure_details=[];self._failure_lock=threading.Lock()
        self.thread=threading.Thread(target=self.loop,daemon=True)
    @property
    def failure_details(self):
        with self._failure_lock:return [dict(detail) for detail in self._failure_details]
    def _record_failure(self,stage,error=None):
        if error is None:reason='ThreadNotStopped'
        elif isinstance(error,EvidenceError):reason='EvidenceError'
        elif isinstance(error,OSError):reason='OSError'
        elif isinstance(error,TypeError):reason='TypeError'
        else:reason='ValueError'
        number=getattr(error,'errno',None) if isinstance(error,OSError) else None
        number=number if type(number) is int and 0<=number<=4095 else None
        with self._failure_lock:
            self.failure=True
            if len(self._failure_details)<3:
                self._failure_details.append({'stage':stage,'reason':reason,'errno':number})
    def scan(self):
        if not self.raw.exists():return
        with self.raw.open('rb') as stream:
            if self.raw.stat().st_size<self.position:raise EvidenceError('Raw client stream was truncated')
            stream.seek(self.position);chunk=stream.read(4*1024*1024);self.position=stream.tell()
        self.pending+=chunk
        lines=self.pending.split(b'\n');self.pending=lines.pop()
        if len(self.pending)>1024*1024:raise EvidenceError('Invalid unterminated raw record')
        for line in lines:
            if line.strip():self.counters.add(self.scenario,json.loads(line))
    def loop(self):
        stage='init-publish'
        try:
            self.counters.publish()
            while not self.stop.wait(1):
                stage='raw-scan';self.scan()
                stage='periodic-publish';self.counters.publish()
        except (OSError,ValueError,TypeError,EvidenceError) as error:self._record_failure(stage,error)
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):
        self.stop.set();self.thread.join(timeout=10)
        if self.thread.is_alive():
            self._record_failure('thread-not-stopped');return
        stage='final-drain'
        try:
            while self.raw.exists() and self.position<self.raw.stat().st_size:self.scan()
            if self.pending.strip():raise EvidenceError('Incomplete raw client result')
            stage='final-publish'
            self.counters.publish()
        except (OSError,ValueError,TypeError,EvidenceError) as error:self._record_failure(stage,error)
