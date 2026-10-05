"""告警演练的安全范围与失败复原行为；外部运行时在边界替身中替代。"""
import importlib.util
import json
import pathlib
import sys
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve()
ROOT = next(p for p in HERE.parents if (p / 'compose.yaml').is_file())
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'scripts'))


def implementation(test):
    path = HERE.with_name('alert_probe.py') if HERE.parent.name == 'alert-probe-development' else ROOT / 'scripts/alert_probe.py'
    if not path.is_file(): test.fail('Missing scoped alert probe implementation')
    spec = importlib.util.spec_from_file_location('tested_alert_probe', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class ScopeTest(unittest.TestCase):
    def test_only_action_specific_isolated_project_and_matching_port_pair_is_accepted(self):
        m = implementation(self)
        recovery = {'OPS_PROJECT_NAME':'contextfence-recovery','ENTRY_PORT':'58096','PROMETHEUS_PORT':'59096'}
        bench = {'OPS_PROJECT_NAME':'contextfence-bench','ENTRY_PORT':'58095','PROMETHEUS_PORT':'59095'}
        self.assertEqual(('contextfence-recovery',58096,59096),m.approved_endpoints(recovery,'metrics'))
        self.assertEqual(('contextfence-recovery',58096,59096),m.approved_endpoints(recovery,'disk'))
        self.assertEqual(('contextfence-bench',58095,59095),m.approved_endpoints(bench,'pool'))
        for config, action in [({},'metrics'),(bench,'disk'),(recovery,'pool'),
                               (dict(bench,PROMETHEUS_PORT='59090'),'pool'),
                               (dict(bench,OPS_PROJECT_NAME='contextfence'),'pool')]:
            with self.assertRaises(m.ProbeError): m.approved_endpoints(config,action)

    def test_local_paths_reject_escape_and_symlink_before_mutation(self):
        m = implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)/'root'; root.mkdir(); (root/'.local').mkdir()
            self.assertEqual(root/'.local/probe/file',m.scoped_local(root,root/'.local/probe/file'))
            with self.assertRaises(m.ProbeError):m.scoped_local(root,root/'.local/../../escape')
            outside=pathlib.Path(directory)/'outside';outside.mkdir()
            try:(root/'.local/link').symlink_to(outside,target_is_directory=True)
            except OSError:return
            with self.assertRaises(m.ProbeError):m.scoped_local(root,root/'.local/link/file')

    def test_actual_container_database_route_and_loopback_ports_are_required(self):
        m=implementation(self)
        project='contextfence-bench'
        data={'Config':{'Labels':{'com.docker.compose.project':project,'com.docker.compose.service':'api-a'},
                        'Env':['CONTEXTFENCE_DATABASE_URL=jdbc:postgresql://recovery-db-abcdef:5432/contextfence']},
              'HostConfig':{'PortBindings':{}},'NetworkSettings':{'Networks':{project+'_backend':{}}}}
        m.require_container(data,project,'api-a',project+'_backend','recovery-db-abcdef')
        with self.assertRaises(m.ProbeError):m.require_container(data,project,'api-a',project+'_backend','postgres')
        data['HostConfig']['PortBindings']={'8080/tcp':[{'HostIp':'0.0.0.0','HostPort':'58095'}]}
        with self.assertRaises(m.ProbeError):m.require_container(data,project,'api-a',project+'_backend','recovery-db-abcdef')
        data['HostConfig']['PortBindings']={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'58095'}]}
        data['Config']['Labels']['com.docker.compose.service']='proxy'
        data['NetworkSettings']['Ports']={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'58095'}]}
        m.require_container(data,project,'proxy',project+'_backend','recovery-db-abcdef',58095)
        data['HostConfig']['PortBindings']['8080/tcp'][0]['HostIp']='0.0.0.0'
        with self.assertRaises(m.ProbeError):m.require_container(data,project,'proxy',project+'_backend','recovery-db-abcdef',58095)

    def test_hidden_actual_host_binding_is_rejected_even_if_requested_binding_is_empty(self):
        m=implementation(self)
        data={'Config':{'Labels':{'com.docker.compose.project':'contextfence-bench','com.docker.compose.service':'postgres'}},
              'HostConfig':{'PortBindings':{}},'NetworkSettings':{'Networks':{'contextfence-bench_backend':{}},
              'Ports':{'5432/tcp':[{'HostIp':'0.0.0.0','HostPort':'5432'}]}}}
        with self.assertRaises(m.ProbeError):m.require_container(data,'contextfence-bench','postgres','contextfence-bench_backend','postgres')

    def test_published_port_must_match_the_actual_running_binding_too(self):
        m=implementation(self)
        data={'Config':{'Labels':{'com.docker.compose.project':'contextfence-bench','com.docker.compose.service':'proxy'}},
              'HostConfig':{'PortBindings':{'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'58095'}]}},
              'NetworkSettings':{'Networks':{'contextfence-bench_backend':{}},'Ports':{'8080/tcp':[{'HostIp':'0.0.0.0','HostPort':'58095'}]}}}
        with self.assertRaises(m.ProbeError):m.require_container(data,'contextfence-bench','proxy','contextfence-bench_backend','postgres',58095)


class RestorationTest(unittest.TestCase):
    def test_termination_interrupt_uses_same_original_file_restoration_path(self):
        m=implementation(self)
        self.assertTrue(callable(getattr(m,'termination_interrupt',None)),'Missing SIGTERM cleanup handler')
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'metric';path.write_bytes(b'original')
            with self.assertRaises(KeyboardInterrupt):
                with m.PreservedFiles([path]):
                    path.write_bytes(b'injected');m.termination_interrupt(15,None)
            self.assertEqual(b'original',path.read_bytes())

    def test_original_bytes_and_absent_files_are_restored_after_injection_failure(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            first=pathlib.Path(directory)/'backup.prom';second=pathlib.Path(directory)/'load.prom'
            original=b'# original\r\ncontextfence_backup_snapshot_timestamp_seconds 123.25\r\n';first.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError,'injected'):
                with m.PreservedFiles([first,second]):
                    first.write_bytes(b'changed');second.write_bytes(b'created');raise RuntimeError('injected')
            self.assertEqual(original,first.read_bytes());self.assertFalse(second.exists())

    def test_restoration_attempts_other_files_when_one_restore_fails_and_reports_failure(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            first=pathlib.Path(directory)/'a';second=pathlib.Path(directory)/'b'
            first.write_bytes(b'A');second.write_bytes(b'B')
            saved=m.atomic_restore
            def restore(path,data,mode):
                if path==first:raise OSError('private path and secret')
                saved(path,data,mode)
            with patch.object(m,'atomic_restore',restore):
                with self.assertRaises(m.ProbeError) as error:
                    with m.PreservedFiles([first,second]):first.write_bytes(b'x');second.write_bytes(b'y')
            self.assertEqual(b'B',second.read_bytes());self.assertNotIn('private',str(error.exception))

    def test_interrupt_during_first_file_restore_still_restores_remaining_files_and_fails(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            first=pathlib.Path(directory)/'a';second=pathlib.Path(directory)/'b';first.write_bytes(b'A');second.write_bytes(b'B')
            original=m.atomic_restore;error=None
            def restore(path,data,mode):
                if path==first:raise KeyboardInterrupt()
                original(path,data,mode)
            try:
                with patch.object(m,'atomic_restore',restore):
                    with m.PreservedFiles([first,second]):first.write_bytes(b'x');second.write_bytes(b'y')
            except BaseException as failure:error=failure
            self.assertEqual(b'B',second.read_bytes())
            self.assertIsInstance(error,m.ProbeError)

    def test_backup_timestamp_rewrite_keeps_other_metrics_and_rejects_missing_or_duplicate(self):
        m=implementation(self)
        raw=b'# gauge\ncontextfence_backup_snapshot_timestamp_seconds 123.0\ncontextfence_backup_last_attempt_success 1\n'
        self.assertEqual(b'# gauge\ncontextfence_backup_snapshot_timestamp_seconds 10\ncontextfence_backup_last_attempt_success 1\n',m.expire_backup(raw,10))
        for raw in (b'other 1\n',b'contextfence_backup_snapshot_timestamp_seconds 1\ncontextfence_backup_snapshot_timestamp_seconds 2\n'):
            with self.assertRaises(m.ProbeError):m.expire_backup(raw,10)

    def test_backup_timestamp_preserves_crlf_while_expiring(self):
        m=implementation(self)
        self.assertEqual(b'contextfence_backup_snapshot_timestamp_seconds 10\r\nother 1\r\n',
                         m.expire_backup(b'contextfence_backup_snapshot_timestamp_seconds 123\r\nother 1\r\n',10))


class DiskBindingTest(unittest.TestCase):
    def test_real_ext4_effective_capacity_sizes_fill_below_actual_available(self):
        m=implementation(self)
        self.assertTrue(callable(getattr(m,'calculate_fill_bytes',None)),'Missing real filesystem sizing')
        self.assertEqual(89920512,m.calculate_fill_bytes(108974080,106266624))
        self.assertEqual(104*1024*1024,m.calculate_fill_bytes(128*1024*1024,128*1024*1024))
        for size,available in [(0,0),(129*1024*1024,100*1024*1024),(108974080,108974081),
                               (108974080,-1),(108974080,4*1024*1024),(True,100),(108974080,True)]:
            with self.assertRaises(m.ProbeError):m.calculate_fill_bytes(size,available)

    def test_backing_filesystem_headroom_guard_precedes_image_and_mkfs_mutation(self):
        m=implementation(self)
        for available_mib in (383,384):
            with self.subTest(available_mib=available_mib),tempfile.TemporaryDirectory() as directory:
                commands=[];root=pathlib.Path(directory);(root/'.local').mkdir()
                def run(args,**kwargs):commands.append(args);raise m.ProbeError('runtime mutation blocked')
                disk=m.DiskLoop(types.SimpleNamespace(root=root,run=run),root/'.local/probe')
                with patch.object(m.os,'name','posix'),patch.object(m.os,'geteuid',return_value=0,create=True), \
                     patch.object(m.os,'statvfs',return_value=types.SimpleNamespace(f_bavail=available_mib,f_frsize=1024*1024),create=True):
                    with self.assertRaises(m.ProbeError):disk.create()
                if available_mib==383:
                    self.assertEqual([],commands);self.assertFalse(disk.image.exists());self.assertFalse(disk.directory.exists())
                else:
                    self.assertEqual('mkfs.ext4',commands[0][0]);self.assertEqual(128*1024*1024,disk.image.stat().st_size)

    def test_sized_fill_stream_writes_exact_final_partial_chunk_and_measures_bound_loop(self):
        m=implementation(self);chunks=[];written=89920512
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local').mkdir();disk=m.DiskLoop(types.SimpleNamespace(root=root),root/'.local/probe')
            disk.device='/dev/loop7';disk.verify=lambda:None
            def write(data):chunks.append(len(data));return len(data)
            writer=types.SimpleNamespace(write=write,flush=lambda:None,fileno=lambda:12)
            stream=__import__('unittest.mock',fromlist=['MagicMock']).MagicMock();stream.__enter__.return_value=writer
            before=types.SimpleNamespace(f_blocks=106420,f_frsize=1024,f_bavail=103776)
            after=types.SimpleNamespace(f_blocks=106420,f_frsize=1024,f_bavail=15963)
            def device(path,**kwargs):return types.SimpleNamespace(st_dev=777,st_rdev=777,st_mode=stat.S_IFBLK,st_size=written)
            with patch.object(m.os,'stat',side_effect=device),patch.object(m.os,'open',side_effect=[11,12]), \
                 patch.object(m.os,'close'),patch.object(m.os,'fstat',return_value=types.SimpleNamespace(st_dev=777,st_size=written)), \
                 patch.object(m.os,'fdopen',return_value=stream),patch.object(m.os,'fsync'), \
                 patch.object(m.os,'fstatvfs',side_effect=[before,after],create=True),patch.object(m.os,'statvfs',return_value=after,create=True), \
                 patch.object(m.os,'O_DIRECTORY',0x10000,create=True),patch.object(m.os,'O_NOFOLLOW',0x20000,create=True), \
                 patch.object(pathlib.Path,'stat',side_effect=lambda *args,**kwargs:device(disk.fill)):
                result=disk.fill_disk()
            self.assertEqual(written,sum(chunks));self.assertEqual(791552,chunks[-1])
            self.assertEqual(written,result['filled_bytes']);self.assertLess(result['available_fraction'],.2)
            self.assertGreaterEqual(after.f_bavail*after.f_frsize,4*1024*1024)

    def test_collector_stop_failure_cannot_skip_independent_loop_cleanup(self):
        m=implementation(self);events=[]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local').mkdir()
            def compose(*args,**kwargs):
                events.append(args)
                if args[0]=='stop':raise m.ProbeError('daemon unavailable')
            ops=types.SimpleNamespace(root=root,compose=compose)
            disk=types.SimpleNamespace(device='/dev/loop7',target=root/'.local/own/mount',mounted=True,
                create=lambda:None,fill_disk=lambda:{'filled_bytes':104*1024*1024},clear_fill=lambda:None,
                cleanup=lambda:events.append(('cleanup',)))
            probe=types.SimpleNamespace(ops=ops,run_id='abc123',event=lambda *a,**kw:None,wait_alerts=lambda *a:None,
                prometheus=lambda *a:{'result':[{'metric':{'mountpoint':str(disk.target)}}]})
            with patch.object(m,'DiskLoop',return_value=disk):
                with self.assertRaises(m.ProbeError):m.disk_probe(probe)
            self.assertIn(('cleanup',),events)
            self.assertEqual(('up','--detach','--no-deps','--no-build','node-exporter'),events[-1])

    def test_mount_disappearing_before_open_cannot_write_to_host_device(self):
        m=implementation(self);written=[]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local').mkdir()
            disk=m.DiskLoop(types.SimpleNamespace(root=root),root/'.local/probe');disk.device='/dev/loop7'
            checks=[]
            def verify():
                checks.append(True)
                if len(checks)>1:raise m.ProbeError('mount disappeared')
            disk.verify=verify
            writer=types.SimpleNamespace(write=lambda data:written.append(len(data)),flush=lambda:None,fileno=lambda:11)
            stream=__import__('unittest.mock',fromlist=['MagicMock']).MagicMock();stream.__enter__.return_value=writer
            def device(path,**kwargs):return types.SimpleNamespace(st_dev=101,st_rdev=777,st_mode=stat.S_IFBLK)
            with patch.object(m.os,'stat',side_effect=device),patch.object(m.os,'open',return_value=11), \
                 patch.object(m.os,'close'),patch.object(m.os,'fstat',return_value=types.SimpleNamespace(st_dev=101)), \
                 patch.object(m.os,'fdopen',return_value=stream),patch.object(m.os,'fsync'),patch.object(pathlib.Path,'open',return_value=stream), \
                 patch.object(m.os,'O_DIRECTORY',0x10000,create=True),patch.object(m.os,'O_NOFOLLOW',0x20000,create=True):
                with self.assertRaises(m.ProbeError):disk.fill_disk()
            self.assertEqual(0,sum(written),'No bytes may reach the host filesystem when the loop mount disappears')

    def test_lost_loop_allocation_response_recovers_only_its_exact_backing_file(self):
        m=implementation(self);commands=[]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local').mkdir()
            def run(args,**kwargs):
                commands.append(args)
                return types.SimpleNamespace(returncode=0,stdout=json.dumps({'loopdevices':[{'name':'/dev/loop7','back-file':str(root/'.local/probe/disk.img')}]}))
            disk=m.DiskLoop(types.SimpleNamespace(root=root,run=run),root/'.local/probe')
            disk.directory.mkdir();disk.image.touch();disk.loop_attempted=True;disk.verify_loop=lambda:None
            disk.cleanup()
            self.assertTrue(any(args[:2]==['losetup','--detach'] and args[-1]=='/dev/loop7' for args in commands))
            self.assertFalse(any('--detach-all' in args for args in commands))

    def test_interrupt_during_resolved_wait_still_cleans_owned_loop_and_restarts_collector(self):
        m=implementation(self);events=[]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local').mkdir()
            ops=types.SimpleNamespace(root=root,compose=lambda *args,**kw:events.append(args))
            disk=types.SimpleNamespace(device='/dev/loop7',target=root/'.local/own/mount',mounted=True,
                create=lambda:None,fill_disk=lambda:{'filled_bytes':104*1024*1024},clear_fill=lambda:events.append(('clear',)),
                cleanup=lambda:events.append(('cleanup',)))
            def wait(names,status,*args):
                if status=='resolved':raise KeyboardInterrupt()
            probe=types.SimpleNamespace(ops=ops,run_id='abc123',event=lambda *a,**kw:None,wait_alerts=wait,
                prometheus=lambda *a:{'result':[{'metric':{'mountpoint':str(disk.target)}}]})
            with patch.object(m,'DiskLoop',return_value=disk):
                with self.assertRaises((m.ProbeError,KeyboardInterrupt)):m.disk_probe(probe)
            self.assertIn(('cleanup',),events)
            self.assertEqual(('up','--detach','--no-deps','--no-build','node-exporter'),events[-1])

    def test_failed_mount_attempt_detaches_own_verified_loop_without_host_unmount(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local').mkdir();commands=[]
            def run(args,**kwargs):
                commands.append(args)
                if args[0]=='findmnt':return types.SimpleNamespace(returncode=1,stdout='')
                return types.SimpleNamespace(returncode=0,stdout='')
            disk=m.DiskLoop(types.SimpleNamespace(root=root,run=run),root/'.local/probe')
            disk.directory.mkdir();disk.target.mkdir();disk.image.touch();disk.device='/dev/loop7';disk.mount_attempted=True
            disk.verify_loop=lambda:None
            disk.cleanup()
            self.assertIsNone(disk.device)
            self.assertTrue(any(args[0]=='findmnt' for args in commands),'Uncertain mount outcome must be checked')
            self.assertFalse(any(args[0]=='umount' for args in commands))

    def test_physical_devices_wrong_backing_and_wrong_mount_are_rejected(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            base=pathlib.Path(directory);image=base/'disk.img';image.touch();target=base/'mount';target.mkdir()
            mount={'filesystems':[{'target':str(target),'source':'/dev/loop8','fstype':'ext4'}]}
            loop={'loopdevices':[{'name':'/dev/loop8','back-file':str(image)}]}
            m.verify_disk_binding(target,image,'/dev/loop8',mount,loop)
            for changed_loop,changed_mount,device in [
                (loop,mount,'/dev/sda'),
                ({'loopdevices':[{'name':'/dev/loop8','back-file':str(base/'other')}]},mount,'/dev/loop8'),
                (loop,{'filesystems':[{'target':str(base),'source':'/dev/loop8','fstype':'ext4'}]},'/dev/loop8'),
                (loop,{'filesystems':[{'target':str(target),'source':'/dev/loop8','fstype':'xfs'}]},'/dev/loop8')]:
                with self.assertRaises(m.ProbeError):m.verify_disk_binding(target,image,device,changed_mount,changed_loop)

    def test_disk_cleanup_revalidates_before_deletion_and_never_unmounts_foreign_filesystem(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local').mkdir();commands=[]
            ops=types.SimpleNamespace(root=root,run=lambda args,**kw:commands.append(args))
            disk=m.DiskLoop(ops,root/'.local/probe')
            disk.directory.mkdir();disk.target.mkdir();disk.image.touch();disk.fill.write_bytes(b'evidence')
            disk.device='/dev/loop7';disk.mounted=True
            disk.verify=lambda:(_ for _ in ()).throw(m.ProbeError('wrong mount'))
            with self.assertRaises(m.ProbeError):disk.cleanup()
            self.assertEqual(b'evidence',disk.fill.read_bytes());self.assertEqual([],commands)


class PoolAndWorkerTest(unittest.TestCase):
    def test_planned_lock_hold_waits_for_real_transaction_completion_before_cleanup(self):
        m=implementation(self);ticks=[];values=iter([None,None,0])
        lock=m.DatabaseLock(types.SimpleNamespace(),'abc123')
        lock.process=types.SimpleNamespace(poll=lambda:next(values));lock.confirmed_at=0
        self.assertTrue(callable(getattr(lock,'complete_hold',None)),'Missing complete real lock duration wait')
        clock=iter([0,0,1,2,3])
        with patch.object(m.time,'monotonic',side_effect=lambda:next(clock)),patch.object(m.time,'sleep'):
            result=lock.complete_hold(lambda:ticks.append(True))
        self.assertEqual(2,len(ticks));self.assertTrue(result['transaction_completed'])

    def test_sidecar_inspect_error_is_not_proof_of_container_absence(self):
        m=implementation(self)
        ops=types.SimpleNamespace(run=lambda args,**kwargs:types.SimpleNamespace(returncode=1 if args[1]=='inspect' else 0,stdout='abcdef012345'))
        sidecar=m.Sidecar(ops,{'project':'contextfence-bench'},'abc123',pathlib.Path('fixture.json'));sidecar.created=True
        with self.assertRaises(m.ProbeError):sidecar.stop()

    def test_frozen_real_threshold_requires_review_and_matching_published_gauge(self):
        m=implementation(self)
        frozen={'format_version':1,'latency_limits_ms':{'p99':120},'baseline_latency_ms':{'p99':100},
                'regression_tolerance':.2,'resource_review':{'resource_stable':True},'environment':{'fixed':True}}
        self.assertEqual(.12,m.frozen_threshold(frozen,b'contextfence_capacity_p99_threshold_seconds 0.12\n'))
        for doc,gauge in [(dict(frozen,resource_review={}),b'contextfence_capacity_p99_threshold_seconds 0.12\n'),
                          (frozen,b'contextfence_capacity_p99_threshold_seconds 0.2\n'),
                          (dict(frozen,latency_limits_ms={'p99':float('nan')}),b'contextfence_capacity_p99_threshold_seconds NaN\n')]:
            with self.assertRaises(m.ProbeError):m.frozen_threshold(doc,gauge)

    def test_sidecar_has_only_verified_backend_readonly_private_mounts_and_no_host_port_or_token(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local/probe').mkdir(parents=True)
            module=root/'alert_probe.py';module.touch();identities=root/'.local/identities.json';identities.touch()
            fixture=root/'.local/probe/fixture.json';fixture.touch()
            args=m.sidecar_command('contextfence-bench','contextfence-bench_backend','abc123',module,identities,fixture)
            joined=' '.join(args)
            self.assertNotIn('--publish',args);self.assertNotIn('--env',args);self.assertNotIn('--privileged',args)
            self.assertEqual(3,joined.count('readonly'));self.assertIn('contextfence-bench_backend',args)
            self.assertIn('--read-only',args);self.assertIn('--cap-drop',args);self.assertIn('--worker',args)
            with self.assertRaises(m.ProbeError):m.sidecar_command('contextfence','contextfence_backend','abc123',module,identities,fixture)

    def test_worker_verdict_and_summary_never_preserve_body_token_or_unknown_error(self):
        m=implementation(self);marker='SYNTHETIC-PRIVATE-CONTENT';token='SYNTHETIC-TOKEN'
        result=m.worker_verdict(503,{'code':'DATABASE_UNAVAILABLE','content':marker,'token':token})
        self.assertEqual({'status':503,'code':'DATABASE_UNAVAILABLE'},result)
        result=m.worker_verdict(599,{'code':marker})
        self.assertEqual({'status':599,'code':'OTHER'},result);self.assertNotIn(marker,json.dumps(result))

    def test_pg_lock_cleanup_terminates_only_current_database_unique_holder_and_waits(self):
        m=implementation(self);calls=[]
        def database(role,sql):calls.append((role,sql));return '0' if role=='monitor' else 't'
        ops=types.SimpleNamespace(database=database)
        lock=m.DatabaseLock(ops,'abc123')
        process=types.SimpleNamespace(poll=lambda:None,wait=lambda timeout:calls.append(('wait',timeout)),terminate=lambda:calls.append(('terminate',)))
        lock.process=process;lock.release()
        sql=calls[0][1]
        self.assertIn('datname=current_database()',sql);self.assertIn("application_name='cf-alertprobe-abc123'",sql)
        self.assertIn("usename='cf_runtime'",sql);self.assertNotIn('cf_admin',sql)
        self.assertIn(('wait',15),calls)

    def test_remaining_pg_holder_after_cleanup_is_reported_as_failure(self):
        m=implementation(self)
        ops=types.SimpleNamespace(database=lambda role,sql:'1' if role=='monitor' else 'f')
        lock=m.DatabaseLock(ops,'abc123');lock.process=types.SimpleNamespace(wait=lambda timeout:None)
        with self.assertRaises(m.ProbeError):lock.release()

    def test_pool_exception_releases_lock_stops_sidecar_and_runs_dual_smoke(self):
        m=implementation(self);events=[]
        ops=types.SimpleNamespace(wait_ready=lambda node,**kw:events.append(('ready',node)),smoke=lambda node:events.append(('smoke',node)),
                                 resume=lambda node:events.append(('resume',node)))
        lock=types.SimpleNamespace(release=lambda:events.append(('release',)))
        sidecar=types.SimpleNamespace(stop=lambda:events.append(('stop',)))
        with self.assertRaisesRegex(RuntimeError,'injected'):
            with m.PoolRecovery(ops,lock,sidecar,lambda:events.append(('ingress',))):raise RuntimeError('injected')
        self.assertEqual(('release',),events[0]);self.assertEqual(('stop',),events[1])
        self.assertIn(('smoke','api-a'),events);self.assertIn(('smoke','api-b'),events);self.assertEqual(('ingress',),events[-1])

    def test_lock_release_failure_still_stops_sidecar_and_checks_both_instances(self):
        m=implementation(self);events=[]
        def release():raise RuntimeError('private failure')
        ops=types.SimpleNamespace(wait_ready=lambda node,**kw:None,smoke=lambda node:events.append(node),resume=lambda node:None)
        with self.assertRaises(m.ProbeError):
            with m.PoolRecovery(ops,types.SimpleNamespace(release=release),types.SimpleNamespace(stop=lambda:events.append('stopped')),lambda:None):pass
        self.assertEqual(['stopped','api-a','api-b'],events)

    def test_failed_recovery_closes_only_owned_project_ingress(self):
        m=implementation(self);events=[]
        def smoke(node):
            if node=='api-a':raise RuntimeError('unsafe safety result')
        ops=types.SimpleNamespace(wait_ready=lambda node,**kw:None,smoke=smoke,resume=lambda node:None,
            close_maintenance=lambda reason:events.append('closed'))
        with self.assertRaises(m.ProbeError):
            with m.PoolRecovery(ops,types.SimpleNamespace(release=lambda:None),types.SimpleNamespace(stop=lambda:None),lambda:None):pass
        self.assertEqual(['closed'],events)

    def pool_lifecycle(self,early_waiting_resolution,remaining_plane=None):
        """Keep real coordination and receiver matching; replace external runtime boundaries."""
        m=implementation(self);events=[];clock={'seconds':0}
        epoch=m.dt.datetime.fromisoformat('2026-10-05T00:00:00+00:00').timestamp()
        names=('ServerFailureRate','DatabasePoolWaiting','DatabasePoolTimeout','TailLatencyRegression')
        def instant(seconds):return m.dt.datetime.fromtimestamp(epoch+seconds,m.dt.timezone.utc).isoformat()
        def advance(seconds):clock['seconds']+=seconds
        identities={name:{'fingerprint':str(index+1)*16,'startsAt':instant(6 if index==3 else 2).replace('+00:00','Z')}
                    for index,name in enumerate(names)}
        def alert(name,status,ended=0):
            return {'status':status,'labels':{'alertname':name,'instance':'api-a'},**identities[name],
                    'endsAt':instant(ended).replace('+00:00','Z')}
        def current_alerts(plane):
            seconds=clock['seconds']
            if seconds<2:return []
            if seconds>=210:
                if plane!=remaining_plane or seconds>=214:return []
                labels={'alertname':'DatabasePoolWaiting','instance':'api-a'}
                return [{'labels':labels,'state':'pending'}] if plane=='prometheus' else \
                       [{'labels':labels,'status':{'state':'suppressed'},**identities['DatabasePoolWaiting']}]
            active=[name for name in names if name!='TailLatencyRegression' or seconds>=6]
            if early_waiting_resolution and seconds>=4:active.remove('DatabasePoolWaiting')
            return [{'labels':{'alertname':name,'instance':'api-a'},'state':'firing'} for name in active] if plane=='prometheus' else \
                   [{'labels':{'alertname':name,'instance':'api-a'},'status':{'state':'active'},**identities[name]} for name in active]
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'.local/metrics').mkdir(parents=True)
            (root/'.local/alerts').mkdir()
            (root/'.local/metrics/capacity.prom').write_bytes(b'contextfence_capacity_p99_threshold_seconds 0.12\n')
            frozen=root/'frozen.json';frozen.write_text(json.dumps({'format_version':1,'latency_limits_ms':{'p99':120},'baseline_latency_ms':{'p99':100},
                'regression_tolerance':.2,'resource_review':{'resource_stable':True},'environment':{'fixed':True}}))
            records=[{'recorded_at':instant(2),'status':'firing','alerts':[alert(name,'firing') for name in names[:3]]},
                     {'recorded_at':instant(6),'status':'firing','alerts':[alert(names[3],'firing')]},
                     {'recorded_at':instant(4 if early_waiting_resolution else 210),'status':'resolved',
                      'alerts':[alert('DatabasePoolWaiting','resolved',4 if early_waiting_resolution else 210)]},
                     {'recorded_at':instant(210),'status':'resolved',
                      'alerts':[alert(name,'resolved',210) for name in names if name!='DatabasePoolWaiting']}]
            (root/'.local/alerts/alerts.jsonl').write_text('\n'.join(json.dumps(row) for row in records)+'\n')
            ops=types.SimpleNamespace(root=root,database=lambda *a:json.dumps({'holders':1,'lock_waiters':2,'blocked_sessions':2}),
                wait_ready=lambda node,**kw:events.append(('ready',node)),smoke=lambda node:events.append(('smoke',node)),
                resume=lambda node:events.append(('resume',node)))
            def complete_hold(sample):clock['seconds']=210;return {'transaction_completed':True}
            lock=types.SimpleNamespace(application='cf-alertprobe-abc123',start=lambda:advance(2),complete_hold=complete_hold,
                release=lambda:events.append(('release',clock['seconds'])))
            sidecar=types.SimpleNamespace(start=lambda:None,sample=lambda:None,stop=lambda:events.append(('stop',clock['seconds'])))
            with patch.object(m,'DatabaseLock',return_value=lock),patch.object(m,'Sidecar',return_value=sidecar), \
                 patch.object(m,'seed_pool_fixture',return_value=root/'.local/fixture.json'), \
                 patch.object(m,'wait_ingress',side_effect=lambda ops:events.append(('ingress',clock['seconds']))), \
                 patch.object(m,'utc_now',side_effect=lambda:instant(clock['seconds'])), \
                 patch.object(m.time,'monotonic',side_effect=lambda:clock['seconds']), \
                 patch.object(m.time,'time',side_effect=lambda:epoch+clock['seconds']),patch.object(m.time,'sleep',side_effect=advance):
                probe=m.Probe(ops,'pool',{'project':'contextfence-bench','prometheus':59095});probe.run_id='abc123'
                probe.prometheus=lambda endpoint='/api/v1/alerts',query=None:{'result':[]} if query else {'alerts':current_alerts('prometheus')}
                probe.alertmanager=lambda:current_alerts('alertmanager')
                failure=None
                try:m.pool_probe(probe,frozen)
                except m.ProbeError as error:failure=str(error)
            return probe.rows,events,failure

    def test_pool_accepts_own_early_resolution_after_other_alert_fires_and_cleanup_clears_all(self):
        rows,events,failure=self.pool_lifecycle(early_waiting_resolution=True)
        self.assertIsNone(failure,'A matched pool recovery earlier than the last firing must remain eligible after cleanup')
        resolved=[row for row in rows if row['phase']=='actual-alert-resolved']
        self.assertEqual({'ServerFailureRate','DatabasePoolWaiting','DatabasePoolTimeout','TailLatencyRegression'},
                         {row['alert'] for row in resolved})
        self.assertTrue(all(row['elapsed_seconds']>=210 for row in resolved))
        self.assertIn(('release',210),events);self.assertIn(('stop',210),events);self.assertIn(('ingress',210),events)
        self.assertIn(('smoke','api-a'),events);self.assertIn(('smoke','api-b'),events)

    def test_pool_waits_for_all_named_pending_or_suppressed_records_to_clear_before_accepting_resolution(self):
        for plane in ('prometheus','alertmanager'):
            with self.subTest(remaining_plane=plane):
                rows,events,failure=self.pool_lifecycle(early_waiting_resolution=False,remaining_plane=plane)
                self.assertIsNone(failure)
                resolved=[row for row in rows if row['phase']=='actual-alert-resolved']
                self.assertEqual(4,len(resolved))
                self.assertTrue(all(row['elapsed_seconds']>=214 for row in resolved),
                                'No pool recovery may be accepted while any named series or manager row remains')


class DeliveryTest(unittest.TestCase):
    def test_resolved_retry_ended_before_this_restoration_cannot_count_as_new_recovery(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'alerts.jsonl'
            path.write_text(json.dumps({'recorded_at':'2026-10-05T00:05:00+00:00','status':'resolved','alerts':[
                {'status':'resolved','labels':{'alertname':'HostDiskLow'},'fingerprint':'aaaaaaaaaaaaaaaa',
                 'startsAt':'2026-10-05T00:00:00Z','endsAt':'2026-10-05T00:01:00Z'}]})+'\n')
            identity={'fingerprint':'aaaaaaaaaaaaaaaa','startsAt':'2026-10-05T00:00:00Z'}
            self.assertIsNone(m.delivered_alert(path,'HostDiskLow','resolved','2026-10-05T00:02:00+00:00',identity))

    def test_old_manager_start_cannot_correlate_with_new_prometheus_firing_series(self):
        m=implementation(self)
        probe=m.Probe(types.SimpleNamespace(root=pathlib.Path('.')),'metrics',{'project':'contextfence-recovery','prometheus':59096})
        labels={'alertname':'BackupExpired'}
        probe.prometheus=lambda:{'alerts':[{'state':'firing','labels':labels}]}
        probe.alertmanager=lambda:[{'labels':labels,'status':{'state':'active'},'fingerprint':'aaaaaaaaaaaaaaaa','startsAt':'2026-10-05T00:00:00Z'}]
        clock=iter([0,0,3]);delivery={'recorded_at':'2026-10-05T00:05:00+00:00','status':'firing','labels':labels}
        with patch.object(m.time,'monotonic',side_effect=lambda:next(clock)),patch.object(m.time,'sleep'),patch.object(m,'delivered_alert',return_value=delivery):
            with self.assertRaises(m.ProbeError):probe.wait_alerts(('BackupExpired',),'firing','2026-10-05T00:01:00+00:00',1)

    def test_same_named_other_disk_delivery_cannot_substitute_for_own_alert_identity(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'alerts.jsonl'
            path.write_text(json.dumps({'recorded_at':'2026-10-05T00:05:00+00:00','status':'firing','alerts':[
                {'status':'firing','labels':{'alertname':'HostDiskLow'},'fingerprint':'aaaaaaaaaaaaaaaa','startsAt':'2026-10-05T00:01:00Z'}]})+'\n')
            identity={'fingerprint':'bbbbbbbbbbbbbbbb','startsAt':'2026-10-05T00:02:00Z'}
            self.assertTrue('identity' in __import__('inspect').signature(m.delivered_alert).parameters,'Missing exact alert identity correlation')
            self.assertIsNone(m.delivered_alert(path,'HostDiskLow','firing','2026-10-05T00:00:00+00:00',identity))
            identity={'fingerprint':'aaaaaaaaaaaaaaaa','startsAt':'2026-10-05T00:01:00Z'}
            self.assertIsNotNone(m.delivered_alert(path,'HostDiskLow','firing','2026-10-05T00:00:00+00:00',identity))

    def test_existing_pending_or_firing_alert_cannot_count_as_new_probe_baseline(self):
        m=implementation(self)
        probe=m.Probe(types.SimpleNamespace(root=pathlib.Path('.')),'metrics',{'project':'contextfence-recovery','prometheus':59096})
        probe.prometheus=lambda:{'alerts':[{'state':'pending','labels':{'alertname':'BackupExpired'}}]}
        self.assertTrue(callable(getattr(probe,'require_baseline',None)),'Missing actual baseline check')
        with self.assertRaises(m.ProbeError):probe.require_baseline(('BackupExpired',))

    def test_delivery_without_prometheus_and_alertmanager_firing_is_not_accepted(self):
        m=implementation(self)
        probe=m.Probe(types.SimpleNamespace(root=pathlib.Path('.')),'metrics',{'project':'contextfence-recovery','prometheus':59096})
        probe.prometheus=lambda:{'alerts':[]};probe.alertmanager=lambda:[]
        clock=iter([0,0,3]);delivery={'recorded_at':'2026-10-05T00:00:00+00:00','status':'firing','labels':{'alertname':'BackupExpired'}}
        with patch.object(m.time,'monotonic',side_effect=lambda:next(clock)),patch.object(m.time,'sleep'),patch.object(m,'delivered_alert',return_value=delivery):
            with self.assertRaises(m.ProbeError):probe.wait_alerts(('BackupExpired',),'firing','2026-10-05T00:00:00+00:00',1)

    def test_delivery_requires_new_actual_receiver_record_and_requested_status(self):
        m=implementation(self)
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'alerts.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in [
                {'recorded_at':'2026-10-05T00:00:00+00:00','status':'firing','alerts':[{'status':'firing','labels':{'alertname':'HostDiskLow'}}]},
                {'recorded_at':'2026-10-05T00:05:00+00:00','status':'resolved','alerts':[{'status':'resolved','labels':{'alertname':'HostDiskLow','mountpoint':'private'}}]}])+'\n')
            self.assertIsNone(m.delivered_alert(path,'HostDiskLow','firing','2026-10-05T00:01:00+00:00'))
            match=m.delivered_alert(path,'HostDiskLow','resolved','2026-10-05T00:01:00+00:00')
            self.assertEqual({'recorded_at':'2026-10-05T00:05:00+00:00','status':'resolved','labels':{'alertname':'HostDiskLow'}},match)


if __name__=='__main__':unittest.main()
