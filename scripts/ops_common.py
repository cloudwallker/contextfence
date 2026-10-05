#!/usr/bin/env python3
"""Local operations primitives. Commands are arrays; errors never print captured output."""
import csv
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import tempfile
import time


class OpsError(Exception):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise OpsError('Duplicate configuration key')
        result[key] = value
    return result


def read_env(path):
    result = {}
    try:
        lines = pathlib.Path(path).read_text(encoding='utf-8-sig').splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'): continue
            if '=' not in line: raise OpsError('Invalid environment configuration')
            key, value = line.split('=', 1)
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or key in result: raise OpsError('Invalid or duplicate environment key')
            if any(c in value for c in ('$','`','\x00','\r','\n')): raise OpsError('Environment expansion is forbidden')
            result[key] = value.strip().strip('\"\'')
    except (OSError, UnicodeError): raise OpsError('Environment configuration is unavailable') from None
    return result


def atomic_bytes(path, data, mode=0o644):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'wb') as file:
            file.write(data); file.flush(); os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(str(path.parent), os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def atomic_json(path, document):
    atomic_bytes(path, (json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def gate_is_open(path):
    try:
        gate = json.loads(pathlib.Path(path).read_text(encoding='utf-8'), object_pairs_hook=strict_object)
        return gate == {'format_version': 1, 'state': 'OPEN'} and type(gate.get('format_version')) is int
    except (OSError, ValueError, TypeError, OpsError): return False


def initialize_ops(root):
    import bootstrap
    root = pathlib.Path(root).resolve()
    bootstrap.bootstrap(root)
    environment = read_env(root / '.env')
    local = root / '.local'
    runtime = local / 'runtime'; secret_dir = local / 'secrets'
    runtime.mkdir(exist_ok=True); secret_dir.mkdir(exist_ok=True)
    local.chmod(0o700); runtime.chmod(0o755); secret_dir.chmod(0o755)
    names = ['db-admin', 'db-migration', 'db-runtime', 'db-backup', 'db-monitor', 'grafana-admin']
    present = [secret_dir.joinpath(name).is_file() for name in names]
    if any(present) and not all(present): raise OpsError('Operational secrets are incomplete; existing files were preserved')
    if not any(present):
        for name in names:
            value = environment['POSTGRES_PASSWORD'] if name == 'db-admin' else secrets.token_hex(32)
            atomic_bytes(secret_dir / name, (value + '\n').encode())
    for name in names:
        if not re.fullmatch(r'[0-9a-f]{64}\n?', (secret_dir / name).read_text()): raise OpsError('Operational secret configuration is invalid')
    identities_path = local / 'identities.json'
    identities = json.loads(identities_path.read_text(), object_pairs_hook=strict_object)
    required = {('ops','metrics'):['READER'],('beta','writer'):['SOURCE_WRITER']}
    for tenant in ('gamma','delta'):
        required[tenant,'writer'] = ['SOURCE_WRITER']
        required[tenant,'alice'] = ['READER','PRODUCER']
    existing = {(p['tenant'],p['subject']):p for p in identities['principals']}
    for identity,roles in required.items():
        if identity in existing and set(existing[identity]['roles']) != set(roles):
            raise OpsError('Operational synthetic identity roles conflict; existing configuration was preserved')
    changed = False
    for (tenant,subject),roles in required.items():
        if (tenant,subject) not in existing:
            principal = {'tenant':tenant,'subject':subject,'roles':roles,'token':secrets.token_hex(32)}
            identities['principals'].append(principal)
            existing[tenant,subject] = principal
            changed = True
    if changed: atomic_json(identities_path, identities)
    metrics = [existing['ops','metrics']]
    atomic_bytes(runtime / 'scrape-token', (metrics[0]['token'] + '\n').encode())
    for name in ('app-gate.json', 'proxy-gate.json'):
        if not (runtime / name).exists(): atomic_json(runtime / name, {'format_version':1,'state':'CLOSED'})
    if not (runtime / 'images.env').exists(): atomic_bytes(runtime / 'images.env', b'API_A_IMAGE=contextfence:local\nAPI_B_IMAGE=contextfence:local\n')
    if not (runtime / 'database.env').exists(): atomic_bytes(runtime / 'database.env', b'DB_HOST=postgres\n')
    (local / 'alerts').mkdir(exist_ok=True)
    (local / 'metrics').mkdir(exist_ok=True)
    (local / 'metrics').chmod(0o755)
    return {'initialized': True, 'gates_open': gate_is_open(runtime / 'proxy-gate.json')}


class Ops:
    def __init__(self, root):
        self.root = pathlib.Path(root).resolve()
        self.runtime = self.root / '.local' / 'runtime'
        self.app_gate = self.runtime / 'app-gate.json'
        self.proxy_gate = self.runtime / 'proxy-gate.json'
        self.deadline = None

    def env(self): return read_env(self.root / '.env')
    def atomic_json(self, path, document): atomic_json(path, document)

    def run(self, args, check=True, capture=True, env=None, input=None, timeout=300):
        if not isinstance(args, (list, tuple)) or not args or any(not isinstance(x, str) for x in args): raise OpsError('Commands require argument arrays')
        args = list(args)
        args[0] = shutil.which(args[0]) or args[0]
        inherited = os.environ.copy()
        if env: inherited.update(env)
        if self.deadline is not None:
            timeout = min(timeout,self.deadline-time.monotonic())
            if timeout <= 0: raise OpsError('Operational deadline exceeded')
        try:
            result = subprocess.run(list(args), cwd=self.root, env=inherited, input=input, capture_output=True, text=True, timeout=timeout, shell=False)
        except (OSError, subprocess.TimeoutExpired): raise OpsError('Operational command unavailable or timed out') from None
        if check and result.returncode: raise OpsError('Operational command failed (exit ' + str(result.returncode) + '); captured output remains private')
        # Always capture: Docker/build/database failures can contain local credentials.
        return result

    def compose_args(self, *args):
        project = read_env(self.root / '.env').get('OPS_PROJECT_NAME','contextfence') if (self.root / '.env').exists() else 'contextfence'
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}',project): raise OpsError('Invalid Compose project name')
        command = [shutil.which('docker') or 'docker','compose','--project-name',project,'--project-directory',str(self.root),'--file',str(self.root / 'compose.yaml')]
        overlay = self.runtime / 'recovery.compose.yaml'
        if overlay.exists(): command.extend(['--file',str(overlay)])
        command.extend(['--env-file',str(self.root / '.env')])
        for name in ('images.env', 'database.env'):
            if (self.runtime / name).exists(): command.extend(['--env-file', str(self.runtime / name)])
        return command + list(args)

    def compose(self, *args, check=True, capture=True, env=None, input=None, timeout=300):
        return self.run(self.compose_args(*args), check=check, capture=capture, env=env, input=input, timeout=timeout)

    def verify_proxy_closed(self):
        result = self.compose('exec','-T','proxy','curl','--silent','--output','/dev/null','--write-out','%{http_code}','http://127.0.0.1:8080/v1/contexts/source', check=False, timeout=10)
        # A stopped/unavailable proxy also cannot expose traffic. Running proxies must return 503.
        if result.returncode == 0 and result.stdout.strip() != '503': raise OpsError('Proxy maintenance verification failed')

    def audit(self, action, reason):
        # Callers use fixed reason identifiers, never exception text.
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', reason): reason = 'operator-action'
        atomic_json(self.runtime / 'last-operation.json', {'recorded_at':utc_now(),'action':action,'reason':reason})

    def close_maintenance(self, reason='operator'):
        atomic_json(self.proxy_gate, {'format_version':1,'state':'CLOSED'})
        atomic_json(self.app_gate, {'format_version':1,'state':'CLOSED'})
        self.verify_proxy_closed()
        self.audit('CLOSED', reason)

    def open_app_gate(self, reason='internal-smoke'):
        atomic_json(self.app_gate, {'format_version':1,'state':'OPEN'})
        self.audit('APP_OPEN_PROXY_UNCHANGED', reason)

    def open_maintenance(self, reason='verified', verified=False):
        if verified is not True: raise OpsError('Opening ingress requires completed safety verification')
        atomic_json(self.app_gate, {'format_version':1,'state':'OPEN'})
        atomic_json(self.proxy_gate, {'format_version':1,'state':'OPEN'})
        self.audit('OPEN_AFTER_VERIFICATION', reason)

    def proxy_command(self, command):
        if not re.fullmatch(r'[a-zA-Z0-9_ /.-]+', command): raise OpsError('Invalid proxy administration command')
        result = self.compose('exec','-T','proxy','sh','-c','printf "%s\\n" "$1" | socat - UNIX-CONNECT:/tmp/haproxy.sock','ops',command, timeout=10)
        if any(word in result.stdout.lower() for word in ('unknown command', 'permission denied', 'no such server')): raise OpsError('Proxy administration rejected command')
        return result.stdout

    def drain(self, instance, timeout=30):
        self._instance(instance)
        self.proxy_command('set server contextfence/' + instance + ' state drain')
        deadline = time.monotonic() + timeout
        while True:
            raw = self.proxy_command('show stat')
            rows = csv.DictReader(io.StringIO(raw.removeprefix('# ')))
            row = next((row for row in rows if row.get('pxname') == 'contextfence' and row.get('svname') == instance), None)
            if row is not None and int(row['scur']) == 0: return
            if time.monotonic() >= deadline: raise OpsError('Drain timed out; server remains drained')
            time.sleep(0.25)

    def resume(self, instance):
        self._instance(instance)
        self.proxy_command('set server contextfence/' + instance + ' state ready')

    @staticmethod
    def _instance(instance):
        if instance not in ('api-a','api-b'): raise OpsError('Unknown API instance')

    def internal_http(self, instance, method, path, payload=None, principal=('acme','alice')):
        self._instance(instance)
        if method not in ('GET','POST') or not re.fullmatch(r'/[A-Za-z0-9_./-]*', path): raise OpsError('Unsupported internal request')
        def quote(value): return '\"' + value.replace('\\','\\\\').replace('\"','\\\"').replace('\n','\\n').replace('\r','\\r') + '\"'
        configuration = ['silent','show-error','max-time = 15','dump-header = "-"','request = ' + quote(method),'url = ' + quote('http://127.0.0.1:8080' + path),'write-out = "\\n%{http_code}"']
        if principal is not None:
            document = json.loads((self.root / '.local' / 'identities.json').read_text(), object_pairs_hook=strict_object)
            token = next((p['token'] for p in document['principals'] if (p['tenant'],p['subject']) == tuple(principal)), None)
            if token is None: raise OpsError('Internal smoke identity unavailable')
            configuration.append('header = ' + quote('Authorization: Bearer ' + token))
        if payload is not None:
            configuration.extend(['header = "Content-Type: application/json"','data = ' + quote(json.dumps(payload, separators=(',',':')))])
        result = self.compose('exec','-T',instance,'curl','--config','-',input='\n'.join(configuration) + '\n',timeout=25)
        try:
            body, status = result.stdout.rsplit('\n',1)
            headers = {}
            while body.startswith('HTTP/'):
                raw_headers, body = body.split('\n\n',1)
                headers = {line.split(':',1)[0].lower():line.split(':',1)[1].strip() for line in raw_headers.splitlines()[1:] if ':' in line}
            return {'status':int(status),'body':json.loads(body, object_pairs_hook=strict_object),'headers':headers}
        except (ValueError, TypeError): raise OpsError('Malformed internal HTTP response') from None

    def wait_ready(self, instance, timeout=120):
        deadline = time.monotonic() + timeout
        if self.deadline is not None: deadline = min(deadline,self.deadline)
        while time.monotonic() < deadline:
            try:
                if self.internal_http(instance,'GET','/health/ready',principal=None)['status'] == 200 and time.monotonic() < deadline: return
            except OpsError: pass
            remaining = deadline - time.monotonic()
            if remaining > 0: time.sleep(min(1,remaining))
        raise OpsError('Candidate readiness timed out')

    def smoke(self, instance):
        from ops_smoke import smoke_instance
        return smoke_instance(self, instance)

    def switch_image(self, instance, image):
        self._instance(instance)
        if not re.fullmatch(r'(?:sha256:[0-9a-f]{64}|[A-Za-z0-9_./:-]+@sha256:[0-9a-f]{64})', image): raise OpsError('Image must be a verified content digest')
        path = self.runtime / 'images.env'
        images = read_env(path)
        images['API_A_IMAGE' if instance == 'api-a' else 'API_B_IMAGE'] = image
        images['API_A_VERSION' if instance == 'api-a' else 'API_B_VERSION'] = image.rsplit('sha256:',1)[-1][:12]
        atomic_bytes(path, ''.join(k + '=' + v + '\n' for k,v in sorted(images.items())).encode())
        self.compose('up','--detach','--no-deps','--no-build',instance, timeout=180)

    def migrate(self, image):
        self.compose('run','--rm','--no-deps','migrate',env={'MIGRATION_IMAGE':image}, timeout=120)
        self.compose('exec','-T','-e','PGHOST=' + self.database_host(),self.database_service(),'sh','/ops/grants.sh',timeout=30)
        self.verify_database_roles()

    def startup_image(self):
        images = read_env(self.runtime / 'images.env')
        return images.get('API_A_IMAGE','contextfence:local')

    def database_host(self):
        path = self.runtime / 'database.env'
        host = read_env(path).get('DB_HOST','postgres') if path.exists() else 'postgres'
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}',host): raise OpsError('Invalid database host')
        return host

    def database_service(self):
        host = self.database_host()
        return host if re.fullmatch(r'recovery-db-[0-9a-f]{6,32}',host) else 'postgres'

    def database_args(self, role, database=None):
        if role not in ('admin','migration','runtime','backup','monitor'): raise OpsError('Invalid database role')
        host = self.database_host()
        service = self.database_service()
        args = ['exec','-T','-e','PGHOST=' + host,service,'sh','/ops/client.sh',role]
        if database:
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,62}',database): raise OpsError('Invalid database name')
            args.append(database)
        return self.compose_args(*args)

    def database(self, role, sql, database=None):
        return self.run(self.database_args(role,database),input=sql,timeout=60).stdout

    def verify_database_roles(self):
        checks = {}
        common = "not (select rolsuper or rolcreatedb or rolcreaterole or rolbypassrls from pg_roles where rolname=current_user)"
        no_ddl = "not has_schema_privilege(current_user,'public','CREATE') and not has_database_privilege(current_user,current_database(),'CREATE')"
        conditions = {
            'migration':common + " and has_schema_privilege(current_user,'public','CREATE')",
            'runtime':common + ' and ' + no_ddl + " and has_table_privilege(current_user,'tenant_guard','UPDATE') and has_table_privilege(current_user,'source_state','INSERT') and has_table_privilege(current_user,'source_state','UPDATE') and has_table_privilege(current_user,'admission_receipts','INSERT') and has_table_privilege(current_user,'flyway_schema_history','SELECT') and not has_table_privilege(current_user,'source_events','DELETE')",
            'backup':common + ' and ' + no_ddl + " and has_table_privilege(current_user,'source_state','SELECT') and has_table_privilege(current_user,'context_items','SELECT') and not has_table_privilege(current_user,'source_state','INSERT') and not has_table_privilege(current_user,'source_state','UPDATE') and not has_table_privilege(current_user,'source_state','DELETE')",
            'monitor':common + ' and ' + no_ddl + " and pg_has_role(current_user,'pg_monitor','USAGE') and not has_table_privilege(current_user,'source_state','SELECT')"
        }
        for role,condition in conditions.items():
            result = self.database(role,"select case when (" + condition + ") then 'OK' else 'DENIED' end;").strip()
            if result != 'OK': raise OpsError('Database role acceptance failed for ' + role)
            checks[role] = 'PASS'
        return checks

    def image_manifest(self):
        services = {}
        for instance in ('api-a','api-b'):
            container = self.compose('ps','--quiet',instance).stdout.strip()
            if not container: raise OpsError('Running image identity is unavailable')
            data = json.loads(self.run(['docker','inspect',container]).stdout)[0]
            services[instance] = {'image_digest':data['Image'],'nano_cpus':data['HostConfig']['NanoCpus'],'memory_bytes':data['HostConfig']['Memory']}
        infrastructure = {}
        for logical_service in ('postgres','proxy','prometheus','grafana','alertmanager','alert-receiver','postgres-exporter','node-exporter'):
            service = self.database_service() if logical_service == 'postgres' else logical_service
            container = self.compose('ps','--quiet',service).stdout.strip()
            if container:
                data = json.loads(self.run(['docker','inspect',container]).stdout)[0]
                infrastructure[logical_service] = {'image_digest':data['Image'],'nano_cpus':data['HostConfig']['NanoCpus'],'memory_bytes':data['HostConfig']['Memory']}
        schema = self.database('backup','select version from flyway_schema_history where success order by installed_rank desc limit 1;').strip()
        engine = json.loads(self.run(['docker','info','--format','{{json .}}']).stdout)
        config_hash = hashlib.sha256()
        for path in [self.root/'compose.yaml',self.root/'Dockerfile'] + sorted((self.root/'ops').rglob('*')):
            if path.is_file():
                config_hash.update(path.relative_to(self.root).as_posix().encode()); config_hash.update(path.read_bytes())
        return {'format_version':1,'recorded_at':utc_now(),'services':services,'infrastructure':infrastructure,'schema_version':schema,'database_host':self.database_host(),'public_config_sha256':config_hash.hexdigest(),'docker':{'server_version':engine.get('ServerVersion'),'cpus':engine.get('NCPU'),'memory_bytes':engine.get('MemTotal'),'operating_system':engine.get('OperatingSystem')},'baseline':{'vm_cpus':engine.get('NCPU'),'vm_memory_gib':engine['MemTotal']/(1024**3) if isinstance(engine.get('MemTotal'),int) else None,'api_cpus':0.75,'api_memory_gib':1,'postgres_cpus':1,'postgres_memory_gib':2}}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['initialize','close','manifest'])
    parser.add_argument('--root',default=str(pathlib.Path(__file__).resolve().parents[1]))
    args = parser.parse_args(); ops = Ops(args.root)
    try:
        if args.action == 'initialize': initialize_ops(args.root)
        elif args.action == 'close': ops.close_maintenance('operator')
        else: atomic_json(ops.root / 'artifacts/local/images.json',ops.image_manifest())
        print('PASS: operational ' + args.action + '; credentials were not printed')
        return 0
    except (OpsError,OSError,ValueError):
        print('FAIL: operational ' + args.action + '; ingress state and local configuration must be checked')
        return 1


if __name__ == '__main__': raise SystemExit(main())
