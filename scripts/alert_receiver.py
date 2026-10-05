#!/usr/bin/env python3
"""Loopback/internal-only Alertmanager sink with bounded, allowlisted records and rotation."""
import datetime as dt
import http.server
import json
import os
import pathlib
import re

LABELS = {'alertname','instance','job','severity','service','name','pool'}


def valid_utc_timestamp(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z',value):
        return False
    try:
        normalized=re.sub(r'\.([0-9]{1,9})(?=Z$)',lambda match:'.'+match[1][:6].ljust(6,'0'),value)
        dt.datetime.fromisoformat(normalized[:-1]+'+00:00');return True
    except ValueError:return False


def sanitize(document):
    status = document.get('status')
    if status not in ('firing','resolved'): raise ValueError('Invalid alert status')
    alerts = []
    for alert in document.get('alerts',[])[:100]:
        labels = {}
        for key,value in alert.get('labels',{}).items():
            if key in LABELS and isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',value): labels[key] = value
        bounded={'status':alert.get('status') if alert.get('status') in ('firing','resolved') else status,'labels':labels}
        fingerprint=alert.get('fingerprint')
        if isinstance(fingerprint,str) and re.fullmatch(r'[0-9a-f]{16}',fingerprint):bounded['fingerprint']=fingerprint
        for key in ('startsAt','endsAt'):
            if valid_utc_timestamp(alert.get(key)):bounded[key]=alert[key]
        alerts.append(bounded)
    return {'recorded_at':dt.datetime.now(dt.timezone.utc).isoformat(),'status':status,'alerts':alerts}


def append_record(directory, record):
    directory = pathlib.Path(directory); directory.mkdir(parents=True,exist_ok=True)
    path = directory / 'alerts.jsonl'
    if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
        for number in range(3,0,-1):
            older = directory / ('alerts.jsonl.' + str(number))
            newer = path if number == 1 else directory / ('alerts.jsonl.' + str(number - 1))
            if newer.exists(): os.replace(newer,older)
    with path.open('a',encoding='utf-8') as file:
        file.write(json.dumps(record,sort_keys=True) + '\n'); file.flush(); os.fsync(file.fileno())


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404); self.end_headers()
    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length','0'))
            if self.path != '/alerts' or not 0 < length <= 262144: raise ValueError()
            record = sanitize(json.loads(self.rfile.read(length)))
            append_record(os.environ.get('ALERT_DIRECTORY','/data'),record)
            self.send_response(202)
        except (ValueError,TypeError,OSError): self.send_response(400)
        self.end_headers()


if __name__ == '__main__':
    http.server.HTTPServer(('0.0.0.0',8088),Handler).serve_forever()
