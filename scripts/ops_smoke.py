#!/usr/bin/env python3
"""Synthetic permission smoke checks; reports contain decisions, never bodies or credentials."""
import argparse
import datetime as dt
import pathlib
import uuid
from ops_common import Ops, OpsError, atomic_json, utc_now


def valid_uuid(value):
    try: return isinstance(value,str) and str(uuid.UUID(value))==value
    except ValueError: return False


def valid_time(value):
    try:
        stamp=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return stamp.tzinfo is not None
    except (ValueError,TypeError,AttributeError): return False


def valid_receipt(receipt,identifiers,decision,sources):
    if not isinstance(receipt,dict) or set(receipt)!={'id','checked_at','decision','context_ids','sources','reasons'}: return False
    if not valid_uuid(receipt['id']) or not valid_time(receipt['checked_at']) or receipt['decision']!=decision or receipt['context_ids']!=identifiers or receipt['sources']!=sources: return False
    reasons=[] if decision=='ALLOWED' else [{'context_id':identifier,'code':decision} for identifier in identifiers]
    return receipt['reasons']==reasons


def smoke_instance(ops, instance):
    try:
        return _smoke_instance(ops,instance)
    except Exception as unavailable:
        try: ops.close_maintenance('smoke-authority-or-safety-failed')
        except Exception: pass
        if isinstance(unavailable,OpsError): raise
        raise OpsError('Candidate permission smoke failed with an invalid response') from None


def _smoke_instance(ops, instance):
    from fixture_producer import append_event
    prefix = 'ops.' + uuid.uuid4().hex
    canary = 'SYNTHETIC-OPS-' + uuid.uuid4().hex
    rows = []
    def call(phase, path, payload=None, principal=('acme','alice'), status=200, code=None):
        reply = ops.internal_http(instance, 'GET' if payload is None else 'POST', path, payload, principal)
        body = reply['body']
        if type(reply['status']) is not int or not isinstance(body,dict): raise OpsError('Candidate permission smoke received an invalid response')
        passed = reply['status'] == status
        passed = passed and 'no-store' in reply.get('headers',{}).get('cache-control','')
        if status >= 400: passed = passed and not body.get('items') and canary not in str(body)
        expected_sources=[{'source_id':prefix,'content_version':1,'auth_epoch':1}]
        reported_code=code
        if code is not None:
            passed=passed and body.get('code')==code
            if path=='/v1/contexts/assemble' and status!=404:
                passed=passed and set(body)=={'status','code','receipt','items'} and body.get('status')==status and valid_receipt(body.get('receipt'),payload['context_ids'],code,expected_sources)
                if code=='ALLOWED': passed=passed and body.get('items')==[{'id':payload['context_ids'][0],'content':canary}]
            else: passed=passed and set(body)=={'code'}
        elif path=='/health/ready':
            reported_code='READY'; passed=passed and body=={'status':'UP'}
        elif path=='/v1/source-events':
            reported_code='APPLIED'
            passed=passed and all(type(body.get(name)) is int for name in ('sequence','content_version','auth_epoch')) and body=={'source_id':prefix,'sequence':payload['sequence'],'outcome':'APPLIED','content_version':1,'auth_epoch':payload['sequence']}
        elif path=='/v1/contexts/source':
            reported_code='CREATED'
            passed=passed and set(body)=={'id','kind','expires_at','depth','sources'} and valid_uuid(body.get('id')) and body.get('kind')=='SOURCE' and type(body.get('depth')) is int and body['depth']==0 and body.get('sources')==expected_sources and valid_time(body.get('expires_at'))
        else: passed=False; reported_code='INVALID_RESPONSE'
        rows.append({'phase':phase,'instance':instance,'expected_status':status,'status':reply['status'] if type(reply['status']) is int else 0,'code':reported_code,'passed':passed})
        if not passed: raise OpsError('Candidate permission smoke failed at ' + phase)
        return body
    fresh = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=240)).isoformat()
    original = {'source_id':prefix,'sequence':1,'content':canary,'readers':['alice'],'state':'ACTIVE','fresh_until':fresh}
    call('ready','/health/ready',principal=None)
    ledger=ops.root / '.local/recovery/ledger.json'
    append_event(ledger,'acme',original)
    first = call('seed','/v1/source-events',original,('acme','writer'))
    replay = call('identical-replay','/v1/source-events',original,('acme','writer'))
    if replay != first: raise OpsError('Same-sequence replay changed its persisted result')
    context = call('create','/v1/contexts/source',{'source_id':prefix,'ttl_seconds':180},status=201)
    request = {'context_ids':[context['id']]}
    for phase in ('allowed','warm'): call(phase,'/v1/contexts/assemble',request,code='ALLOWED')
    call('cross-tenant','/v1/contexts/assemble',request,('beta','alice'),404,'NOT_FOUND')
    revoked=dict(original,sequence=2,readers=[])
    append_event(ledger,'acme',revoked)
    call('revoke','/v1/source-events',revoked,('acme','writer'))
    call('post-revoke','/v1/contexts/assemble',request,status=403,code='SOURCE_ACCESS_DENIED')
    call('replay-old','/v1/source-events',original,('acme','writer'))
    call('old-event-no-resurrection','/v1/contexts/assemble',request,status=403,code='SOURCE_ACCESS_DENIED')
    call('sequence-conflict','/v1/source-events',dict(original,sequence=2),('acme','writer'),409,'EVENT_CONFLICT')
    regranted=dict(original,sequence=3)
    append_event(ledger,'acme',regranted)
    call('regrant','/v1/source-events',regranted,('acme','writer'))
    call('old-epoch','/v1/contexts/assemble',request,status=409,code='CONTEXT_STALE')
    return {'format_version':1,'recorded_at':utc_now(),'all_passed':True,'observations':rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument('--instance',choices=['api-a','api-b','both'],default='both')
    parser.add_argument('--output',default='artifacts/local/ops-smoke.json')
    parser.add_argument('--resume',action='store_true',help='After BOTH instances pass, enable backends; persistent ingress gate is unchanged')
    args = parser.parse_args(); ops = Ops(args.root)
    try:
        reports = [smoke_instance(ops,node) for node in (('api-a','api-b') if args.instance == 'both' else (args.instance,))]
        if args.resume:
            if args.instance != 'both': raise OpsError('Both instances must pass before backend recovery')
            for node in ('api-a','api-b'): ops.resume(node)
        atomic_json(ops.root / args.output, {'recorded_at':utc_now(),'all_passed':True,'instances':reports})
        print('PASS: synthetic internal permission, epoch, tenant and replay smoke')
        return 0
    except (OpsError,OSError,ValueError):
        print('FAIL: internal safety smoke; response bodies and credentials were not printed')
        return 1


if __name__ == '__main__': raise SystemExit(main())
