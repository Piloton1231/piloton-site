"""Run on the VPS over SSH: python3 issue_private_niconico.py sm12345678.

Requires root/Docker access. Only this isolated short-lived container sees the
browser profile. The public resolver only mounts opaque playback tickets read-only.
"""
import json
import os
import re
import subprocess
import sys

if len(sys.argv) != 2 or not re.fullmatch(r'(?:sm|so|nm)\d+', sys.argv[1]):
    raise SystemExit('Usage: issue_private_niconico.py VIDEO_ID')
info = json.loads(subprocess.check_output(['docker', 'inspect', 'piloton-video-resolver-1']))[0]
config = dict(x.split('=', 1) for x in info['Config']['Env'] if '=' in x)
env = os.environ.copy()
env['JAPAN_PROXY_URL'] = config.get('JAPAN_PROXY_URL', '')
if not env['JAPAN_PROXY_URL']:
    raise SystemExit('Japan proxy is not configured')
result = subprocess.run([
    'docker', 'run', '--rm', '--user', '0', '--memory', '384m',
    '--memory-swap', '384m', '--cpus', '0.5', '--pids-limit', '64', '-e', 'JAPAN_PROXY_URL',
    '-v', '/home/nico-browser/profile:/browser-profile:ro',
    '-v', '/opt/piloton-private-niconico:/run/private-niconico',
    '--entrypoint', 'python3', 'piloton-video-resolver',
    '/resolver/private_niconico.py', sys.argv[1],
], env=env)
sys.exit(result.returncode)
