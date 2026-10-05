import hashlib
import importlib
import json
import pathlib
import sys
import tempfile
import unittest
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from ops_common import Ops, atomic_json


class FixtureLedgerTest(unittest.TestCase):
    def module(self):
        self.assertTrue((ROOT / 'scripts/fixture_producer.py').exists(), 'Independent trusted fixture producer is missing')
        return importlib.import_module('fixture_producer')

    def test_producer_atomically_records_full_history_before_database_application(self):
        producer = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            ledger = pathlib.Path(temporary) / 'ledger.json'
            event = {'source_id': 'policy', 'sequence': 1, 'content': 'SYNTHETIC', 'readers': ['bob', 'alice'], 'state': 'ACTIVE', 'fresh_until': '2026-10-04T00:00:00Z'}
            producer.append_event(ledger, 'acme', event)
            producer.append_event(ledger, 'acme', dict(event, sequence=2, readers=['bob']))
            document = json.loads(ledger.read_text())
            self.assertEqual([1, 2], [row['sequence'] for row in document['events']])
            self.assertEqual(['bob', 'alice'], document['events'][0]['readers'])
            expected = hashlib.sha256(json.dumps(document['events'], sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            self.assertEqual(expected, document['events_sha256'])
            original = ledger.read_bytes()
            with self.assertRaises(producer.OpsError): producer.append_event(ledger, 'acme', dict(event, sequence=4))
            self.assertEqual(original, ledger.read_bytes())

    def test_producer_never_derives_authority_from_database_or_rewrites_old_history(self):
        producer = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            ledger = pathlib.Path(temporary) / 'ledger.json'
            event = {'source_id': 'policy', 'sequence': 1, 'content': '', 'readers': [], 'state': 'DELETED', 'fresh_until': '2026-10-04T00:00:00Z'}
            producer.append_event(ledger, 'acme', event)
            original = ledger.read_bytes()
            with self.assertRaises(producer.OpsError): producer.append_event(ledger, 'acme', dict(event, sequence=2, state='ACTIVE', content='SYNTHETIC'))
            self.assertEqual(original, ledger.read_bytes())

    def test_rotated_instances_are_warmed_again_before_the_database_fault(self):
        producer=self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary); ledger=root/'ledger.json'; fixture=root/'fixture.json'
            original={'source_id':'revoke','sequence':1,'content':'SYNTHETIC','readers':['alice'],'state':'ACTIVE','fresh_until':'2026-10-04T00:00:00Z'}
            producer.append_event(ledger,'acme',original); producer.append_event(ledger,'acme',dict(original,source_id='delete'))
            atomic_json(fixture,{'format_version':1,'prefix':'recovery.synthetic','sources':{'keep':'keep','revoke':'revoke','delete':'delete'},
                'old_source':'old-source','old_derived':'old-derived','old_context_ids':['old-source','old-derived'],
                'old_receipt':'old-receipt','markers':['SYNTHETIC','SYNTHETIC-DERIVED'],'committed_contexts':[]})
            atomic_json(root/'.local/identities.json',{'principals':[{'tenant':'acme','subject':'alice','token':'SYNTHETIC-OLD-TOKEN-1234567890'}]})
            class Services(Ops):
                def __init__(self): super().__init__(root); self.calls=[]
                def verify_proxy_closed(self): pass
                def compose(self,*args,**kwargs): return types.SimpleNamespace(stdout='401' if args[-2:]==('--config','-') else '')
                def wait_ready(self,*args,**kwargs): pass
                def internal_http(self,instance,method,path,payload=None,principal=('acme','alice')):
                    self.calls.append((instance,path))
                    if path=='/v1/source-events': body={'outcome':'APPLIED'}; status=200
                    elif path=='/v1/contexts/source': body={'id':'new'}; status=201
                    else: body={'code':'ALLOWED','receipt':{'id':'receipt-'+str(len(self.calls))}}; status=200
                    return {'status':status,'body':body,'headers':{'cache-control':'no-store'}}
            client=Services(); producer.mutate_fixture(client,ledger,fixture)
            for instance in ('api-a','api-b'):
                self.assertEqual(2,client.calls.count((instance,'/v1/contexts/assemble')))


if __name__ == '__main__': unittest.main()
