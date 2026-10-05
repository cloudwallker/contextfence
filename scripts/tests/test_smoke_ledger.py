import json
import pathlib
import sys
import tempfile
import unittest
import datetime as dt

ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from ops_common import Ops,OpsError,gate_is_open
from ops_smoke import smoke_instance


class SmokeLedgerTest(unittest.TestCase):
    def services(self,directory):
        class Services(Ops):
            def __init__(self): super().__init__(directory); self.events={}; self.current=None
            def verify_proxy_closed(self): pass
            def internal_http(self,instance,method,path,payload=None,principal=('acme','alice')):
                status=200; body={}
                if path=='/health/ready': body={'status':'UP'}
                elif path=='/v1/source-events':
                    previous=self.events.get(payload['sequence'])
                    if previous:
                        if previous[0]!=payload: status=409; body={'code':'EVENT_CONFLICT'}
                        else: body=previous[1]
                    else:
                        self.current=payload.copy()
                        body={'source_id':payload['source_id'],'sequence':payload['sequence'],'outcome':'APPLIED','content_version':1,'auth_epoch':payload['sequence']}
                        self.events[payload['sequence']]=(payload.copy(),body.copy())
                elif path=='/v1/contexts/source':
                    status=201; body={'id':'00000000-0000-0000-0000-000000000001','kind':'SOURCE','expires_at':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=180)).isoformat(),
                        'depth':0,'sources':[{'source_id':self.current['source_id'],'content_version':1,'auth_epoch':1}]}
                elif path=='/v1/contexts/assemble':
                    if principal[0]=='beta': status=404; body={'code':'NOT_FOUND'}
                    elif not self.current['readers']: status=403; body={'code':'SOURCE_ACCESS_DENIED','items':[]}
                    elif self.current['sequence']>1: status=409; body={'code':'CONTEXT_STALE','items':[]}
                    else: body={'code':'ALLOWED','items':[{'id':payload['context_ids'][0],'content':self.current['content']}]}
                    if principal[0]!='beta':
                        body['status']=status
                        body['receipt']={'id':'00000000-0000-0000-0000-000000000002','checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                            'decision':body['code'],'context_ids':payload['context_ids'],'sources':[{'source_id':self.current['source_id'],'content_version':1,'auth_epoch':1}],
                            'reasons':[] if status==200 else [{'context_id':payload['context_ids'][0],'code':body['code']}]}
                else: raise AssertionError('Unexpected external HTTP request')
                return {'status':status,'body':body,'headers':{'cache-control':'no-store'}}
        return Services()

    def test_smoke_records_all_authority_changes_but_not_replay_or_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            client=self.services(directory); report=smoke_instance(client,'api-a')
            ledger=client.root/'.local/recovery/ledger.json'
            self.assertTrue(ledger.exists(),'Smoke sources are missing from the independent recovery ledger')
            events=json.loads(ledger.read_text())['events']
            self.assertEqual([1,2,3],[row['sequence'] for row in events])
            self.assertEqual([['alice'],[],['alice']],[row['readers'] for row in events])
            self.assertEqual(['acme']*3,[row['tenant'] for row in events])
            self.assertTrue(report['all_passed'])

    def test_corrupt_ledger_prevents_source_write_and_closes_both_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            client=self.services(directory); client.open_maintenance('test',verified=True)
            ledger=client.root/'.local/recovery/ledger.json'; ledger.parent.mkdir(parents=True,exist_ok=True)
            ledger.write_text('{"format_version":1,"complete":false}')
            with self.assertRaises(OpsError): smoke_instance(client,'api-a')
            self.assertFalse(client.events)
            self.assertFalse(gate_is_open(client.app_gate)); self.assertFalse(gate_is_open(client.proxy_gate))
            self.assertEqual('{"format_version":1,"complete":false}',ledger.read_text())

    def test_ready_code_injection_and_incomplete_success_are_rejected_without_echoing(self):
        marker='PRIVATE-RESPONSE-MARKER'
        for mutation in ('ready-code','ready-empty','seed-empty','create-incomplete','allowed-receipt-missing'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as directory:
                client=self.services(directory); client.open_maintenance('test',verified=True)
                original=client.internal_http
                def changed(instance,method,path,payload=None,principal=('acme','alice')):
                    reply=original(instance,method,path,payload,principal)
                    if mutation=='ready-code' and path=='/health/ready': reply['body']['code']=marker
                    elif mutation=='ready-empty' and path=='/health/ready': reply['body']={}
                    elif mutation=='seed-empty' and path=='/v1/source-events' and payload['sequence']==1: reply['body']={'outcome':'APPLIED'}
                    elif mutation=='create-incomplete' and path=='/v1/contexts/source': reply['body']={'id':reply['body']['id']}
                    elif mutation=='allowed-receipt-missing' and reply['body'].get('code')=='ALLOWED': reply['body'].pop('receipt',None)
                    return reply
                client.internal_http=changed
                with self.assertRaises(OpsError) as failure: smoke_instance(client,'api-a')
                self.assertNotIn(marker,str(failure.exception))
                self.assertFalse(gate_is_open(client.proxy_gate)); self.assertFalse(gate_is_open(client.app_gate))


if __name__=='__main__': unittest.main()
