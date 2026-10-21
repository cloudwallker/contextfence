"""实际收集端保留有限告警身份，从同名事件中区分本轮交付。"""
import importlib.util
import http.client
import http.server
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

HERE=pathlib.Path(__file__).resolve()
ROOT=next(p for p in HERE.parents if (p/'compose.yaml').is_file())
sys.path.insert(0,str(ROOT/'scripts'))
import alert_receiver


class ReceiverIdentityTest(unittest.TestCase):
    def test_foreign_disk_same_named_webhook_cannot_represent_own_alert(self):
        path=HERE.with_name('alert_probe.py') if HERE.parent.name=='alert-probe-development' else ROOT/'scripts/alert_probe.py'
        spec=importlib.util.spec_from_file_location('identity_alert_probe',path);probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
        def webhook(fingerprint,start):
            return {'status':'firing','alerts':[{'status':'firing','labels':{'alertname':'HostDiskLow','mountpoint':'PRIVATE-HOST-PATH'},
                'fingerprint':fingerprint,'startsAt':start,'endsAt':'2026-10-05T00:06:00Z','annotations':{'body':'PRIVATE-SECRET'}}]}
        identity={'fingerprint':'bbbbbbbbbbbbbbbb','startsAt':'2026-10-05T00:02:00Z'}
        with tempfile.TemporaryDirectory() as directory:
            sink=pathlib.Path(directory)/'alerts.jsonl'
            alert_receiver.append_record(directory,alert_receiver.sanitize(webhook('aaaaaaaaaaaaaaaa','2026-10-05T00:01:00Z')))
            self.assertIsNone(probe.delivered_alert(sink,'HostDiskLow','firing','2026-10-05T00:00:00+00:00',identity))
            alert_receiver.append_record(directory,alert_receiver.sanitize(webhook('bbbbbbbbbbbbbbbb','2026-10-05T00:02:00Z')))
            self.assertIsNotNone(probe.delivered_alert(sink,'HostDiskLow','firing','2026-10-05T00:00:00+00:00',identity),'Own identity must survive actual receiver sanitation')
            raw=sink.read_text();self.assertNotIn('PRIVATE-HOST-PATH',raw);self.assertNotIn('PRIVATE-SECRET',raw)

    def test_only_valid_fingerprint_and_calendar_valid_utc_timestamps_survive(self):
        valid={'status':'resolved','labels':{'alertname':'HostDiskLow'},'fingerprint':'0123456789abcdef',
               'startsAt':'2026-10-05T00:01:02.123456789Z','endsAt':'2026-10-05T00:02:03Z'}
        clean=alert_receiver.sanitize({'status':'resolved','alerts':[valid]})['alerts'][0]
        self.assertEqual('0123456789abcdef',clean.get('fingerprint'))
        self.assertEqual('2026-10-05T00:01:02.123456789Z',clean.get('startsAt'))
        self.assertEqual('2026-10-05T00:02:03Z',clean.get('endsAt'))
        for invalid in ('2026-02-30T00:00:00Z','2026-10-05T00:00:00+08:00','PRIVATE-PATH-TOKEN','2026-10-05T00:00:00.1234567890Z'):
            row=alert_receiver.sanitize({'status':'firing','alerts':[dict(valid,fingerprint='PRIVATE-TOKEN',startsAt=invalid,endsAt=invalid)]})['alerts'][0]
            self.assertNotIn('fingerprint',row);self.assertNotIn('startsAt',row);self.assertNotIn('endsAt',row)


class ReceiverHttpValidationTest(unittest.TestCase):
    def test_malformed_json_shapes_return_400_and_valid_followup_is_recorded(self):
        invalid_documents = [
            [], None, 'invalid-document', 1, True,
            {'status':'firing','alerts':None},
            {'status':'firing','alerts':{}},
            {'status':'firing','alerts':'invalid-alerts'},
            {'status':'firing','alerts':[None]},
            {'status':'firing','alerts':[[]]},
            {'status':'firing','alerts':['invalid-alert']},
            {'status':'firing','alerts':[1]},
            {'status':'firing','alerts':[{'labels':[]}]},
            {'status':'firing','alerts':[{'labels':None}]},
            {'status':'firing','alerts':[{'labels':'invalid-labels'}]},
            {'status':'firing','alerts':[{'labels':1}]},
        ]
        valid_document = {'status':'firing','alerts':[{
            'status':'firing','labels':{'alertname':'HostDiskLow','mountpoint':'PRIVATE-PATH'},
            'fingerprint':'0123456789abcdef','startsAt':'2026-10-05T00:01:02Z',
            'annotations':{'body':'PRIVATE-SECRET'},
        }]}

        class QuietServer(http.server.HTTPServer):
            def handle_error(self, request, client_address):
                pass

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ALERT_DIRECTORY':directory}):
            server = QuietServer(('127.0.0.1',0),alert_receiver.Handler)
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                def post(document):
                    connection = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                    try:
                        connection.request('POST','/alerts',json.dumps(document),{'Content-Type':'application/json'})
                        try:
                            response = connection.getresponse()
                        except http.client.RemoteDisconnected:
                            self.fail('Malformed JSON shape closed the connection instead of returning HTTP 400')
                        status = response.status
                        response.read()
                        return status
                    finally:
                        connection.close()

                sink = pathlib.Path(directory)/'alerts.jsonl'
                accepted = 0
                for document in invalid_documents:
                    with self.subTest(document=document):
                        before = sink.read_bytes() if sink.exists() else None
                        self.assertEqual(400,post(document))
                        self.assertEqual(before,sink.read_bytes() if sink.exists() else None)
                        self.assertEqual(202,post(valid_document))
                        accepted += 1
                        records = [json.loads(line) for line in sink.read_text(encoding='utf-8').splitlines()]
                        self.assertEqual(accepted,len(records))
                        self.assertEqual('firing',records[-1]['status'])
                        self.assertEqual([{'status':'firing','labels':{'alertname':'HostDiskLow'},
                            'fingerprint':'0123456789abcdef','startsAt':'2026-10-05T00:01:02Z'}],records[-1]['alerts'])
                        self.assertNotIn('PRIVATE-',sink.read_text(encoding='utf-8'))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__=='__main__':unittest.main()
