"""Test-only CLI boundary: never connects to Docker or a real database."""
import json
import os
import pathlib
import sys
import uuid
import datetime as dt

args = sys.argv[1:]
record = pathlib.Path(os.environ['DOCKER_CALL_RECORD'])
with record.open('a') as file: file.write(json.dumps(args) + '\n')
if 'up' in args or 'down' in args:
    code = int(os.environ.get('DOCKER_MUTATION_EXIT','0'))
    if code: print('controlled Docker failure',file=sys.stderr)
    raise SystemExit(code)
if 'image' in args and 'inspect' in args: print(json.dumps([{'Id':'sha256:' + 'a'*64}]))
elif 'inspect' in args: print(json.dumps([{'Image':'sha256:'+'a'*64,'HostConfig':{'NanoCpus':750000000,'Memory':1073741824}}]))
elif 'info' in args: print(json.dumps({'ServerVersion':'test-only','NCPU':4,'MemTotal':8589934592,'OperatingSystem':'test-boundary'}))
elif 'ps' in args and '--quiet' in args: print('fake-' + args[-1])
elif '/ops/client.sh' in args:
    query = sys.stdin.read()
    print('OK' if "then 'OK'" in query else '2')
elif 'socat' in ' '.join(args):
    if args[-1] == 'show stat': print('# pxname,svname,scur\ncontextfence,api-a,0\ncontextfence,api-b,0')
elif 'curl' in args and '--config' not in args: print('503')
elif 'curl' in args and '--config' in args:
    config = sys.stdin.read().splitlines()
    url = next(json.loads(line.split(' = ',1)[1]) for line in config if line.startswith('url = '))
    path = url.split(':8080',1)[1]
    payload = next((json.loads(json.loads(line.split(' = ',1)[1])) for line in config if line.startswith('data = ')),None)
    root = pathlib.Path(args[args.index('--project-directory')+1])
    token = next((json.loads(line.split(' = ',1)[1]).split('Bearer ',1)[1] for line in config if line.startswith('header = ') and 'Bearer ' in line),None)
    identities = json.loads((root/'.local/identities.json').read_text())['principals']
    tenant = next((p['tenant'] for p in identities if p['token'] == token),'none')
    state_path = record.with_name('fake-state.json')
    state = json.loads(state_path.read_text()) if state_path.exists() else {'events':{},'sources':{},'contexts':{}}
    status = 200
    body = {'status':'UP'}
    if path == '/v1/source-events':
        key = tenant + '/' + payload['source_id']
        event_key = key + '/' + str(payload['sequence'])
        old = state['events'].get(event_key)
        if old:
            if old['payload'] != payload: status,body = 409,{'code':'EVENT_CONFLICT'}
            else: body = old['result']
        else:
            current = state['sources'].get(key)
            body = {'source_id':payload['source_id'],'sequence':payload['sequence'],'outcome':'APPLIED','content_version':1,'auth_epoch':payload['sequence']}
            if current and payload['sequence'] < current['sequence']: body['outcome'] = 'IGNORED_STALE'
            else: state['sources'][key] = payload
            state['events'][event_key] = {'payload':payload,'result':body}
    elif path == '/v1/contexts/source':
        identity = str(uuid.uuid4())
        state['contexts'][identity] = {'tenant':tenant,'source':payload['source_id'],'epoch':state['sources'][tenant+'/'+payload['source_id']]['sequence']}
        status,body = 201,{'id':identity,'kind':'SOURCE','expires_at':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=payload['ttl_seconds'])).isoformat(),'depth':0,
            'sources':[{'source_id':payload['source_id'],'content_version':1,'auth_epoch':state['sources'][tenant+'/'+payload['source_id']]['sequence']}]}
    elif path == '/v1/contexts/assemble':
        item = state['contexts'][payload['context_ids'][0]]
        source = state['sources'][item['tenant']+'/'+item['source']]
        if tenant != item['tenant']: status,body = 404,{'code':'NOT_FOUND'}
        elif not source['readers']: status,body = 403,{'code':'SOURCE_ACCESS_DENIED','items':[]}
        elif item['epoch'] != source['sequence']: status,body = 409,{'code':'CONTEXT_STALE','items':[]}
        else: body = {'code':'ALLOWED','items':[{'id':payload['context_ids'][0],'content':source['content']}]}
        if tenant==item['tenant']:
            body['status']=status
            body['receipt']={'id':str(uuid.uuid4()),'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'decision':body['code'],'context_ids':payload['context_ids'],
                'sources':[{'source_id':item['source'],'content_version':1,'auth_epoch':item['epoch']}],
                'reasons':[] if status==200 else [{'context_id':payload['context_ids'][0],'code':body['code']}]}
    state_path.write_text(json.dumps(state))
    print('HTTP/1.1 ' + str(status) + '\nCache-Control: no-store\nContent-Type: application/json\n')
    print(json.dumps(body)); print(status,end='')
