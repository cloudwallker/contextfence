#!/usr/bin/env python3
"""Start the complete local stack while persistent ingress remains closed until safety checks pass."""
import argparse
import json
import os
import pathlib
import re
import sys
from fetch_grafana import ensure_archive
from ops_common import Ops, OpsError, atomic_bytes, atomic_json, initialize_ops, read_env, strict_object, utc_now

GRAFANA_BUILD_IMAGE = 'contextfence-grafana:12.2.0-official-archive'
IMAGE_ID_PATTERN = r'sha256:[0-9a-f]{64}'
IMAGE_REFERENCE_PATTERN = r'(?:sha256:[0-9a-f]{64}|[A-Za-z0-9_./:-]+@sha256:[0-9a-f]{64})'


class GrafanaImageConfigurationError(OpsError):
    """Only this fixed safe diagnostic is suitable for displaying at startup."""


def prepare_grafana_image(ops):
    """Resolve Grafana to a verified local image ID or an existing immutable reference."""
    path = ops.runtime / 'images.env'
    runtime = read_env(path)
    configured = os.environ.get('GRAFANA_IMAGE', runtime.get('GRAFANA_IMAGE', ops.env().get('GRAFANA_IMAGE', '')))
    image = configured or GRAFANA_BUILD_IMAGE
    if image == GRAFANA_BUILD_IMAGE:
        ensure_archive(ops.root)
        ops.compose('--profile', 'tools', 'build', 'grafana-build', timeout=900)
    elif not re.fullmatch(IMAGE_REFERENCE_PATTERN, image):
        raise GrafanaImageConfigurationError('Grafana image must be an existing content digest; mutable external tags are forbidden')
    result = ops.run(['docker', 'image', 'inspect', image], timeout=30)
    try:
        images = json.loads(result.stdout, object_pairs_hook=strict_object)
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise ValueError()
        identity = images[0].get('Id')
        if not isinstance(identity, str) or not re.fullmatch(IMAGE_ID_PATTERN, identity):
            raise ValueError()
        if image.startswith('sha256:') and identity != image:
            raise ValueError()
        if '@sha256:' in image:
            repo_digests = images[0].get('RepoDigests')
            if not isinstance(repo_digests, list) or not all(isinstance(item, str) for item in repo_digests) or image not in repo_digests:
                raise ValueError()
    except (ValueError, TypeError, OpsError):
        raise OpsError('Grafana image content identity could not be verified') from None
    fixed = identity if image == GRAFANA_BUILD_IMAGE else image
    # Preserve every unrelated byte, including comments, API pins and local settings.
    original = path.read_bytes()
    bom = b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b''
    lines = original.decode('utf-8-sig').splitlines(keepends=True)
    lines = [line for line in lines if line.strip().split('=', 1)[0] != 'GRAFANA_IMAGE']
    body = ''.join(lines)
    if body and not body.endswith(('\n', '\r')):
        body += '\n'
    atomic_bytes(path, bom + (body + 'GRAFANA_IMAGE=' + fixed + '\n').encode('utf-8'), mode=0o600)
    return fixed


def start_stack(ops):
    ops.close_maintenance('startup-preflight')
    grafana = prepare_grafana_image(ops)
    grafana_env = {'GRAFANA_IMAGE': grafana}
    image = ops.startup_image()
    builds = ['proxy','alert-receiver']
    if image == 'contextfence:local': builds.insert(0,'api-a')
    ops.compose('build',*builds,env=grafana_env,timeout=900)
    ops.compose('up','--detach','--wait','--wait-timeout','120','--no-deps',ops.database_service(),env=grafana_env,timeout=150)
    ops.migrate(image)
    ops.open_app_gate('startup-internal-smoke')
    ops.compose('up','--detach','--wait','--wait-timeout','180','--no-build','api-a','api-b','proxy','prometheus','grafana','alertmanager','alert-receiver','postgres-exporter','node-exporter',env=grafana_env,timeout=210)
    for node in ('api-a','api-b'):
        ops.wait_ready(node,timeout=120)
        ops.smoke(node)
    for node in ('api-a','api-b'): ops.resume(node)
    ops.open_maintenance('startup-safety-passed',verified=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=str(pathlib.Path(__file__).resolve().parents[1]))
    args = parser.parse_args(); ops = Ops(args.root)
    try:
        initialize_ops(ops.root)
        ops.compose('version','--short',timeout=10)
        start_stack(ops)
        atomic_json(ops.root / 'artifacts/local/startup.json',{'recorded_at':utc_now(),'outcome':'SAFETY_PASSED','images':ops.image_manifest()})
        print('ContextFence startup safety passed; the unified local ingress is open.')
        return 0
    except Exception as error:
        try: ops.close_maintenance('startup-failed')
        except Exception: pass
        if isinstance(error, GrafanaImageConfigurationError):
            print('Startup failed: Grafana image must be an existing content digest; mutable external tags are forbidden. Ingress remains closed.', file=sys.stderr)
        else:
            print('Startup failed: incomplete configuration, container failure or safety verification failure. Ingress remains closed; private outputs were not printed.',file=sys.stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
