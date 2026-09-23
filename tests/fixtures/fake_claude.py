"""Account-free Claude stand-in with actual session persistence semantics."""
import json
import os
from pathlib import Path
import re
import sys
import time

args = sys.argv[1:]
flag = '--resume' if '--resume' in args else '--session-id'
session = args[args.index(flag) + 1]
storage = Path(os.environ['CLAUDE_CONFIG_DIR'])
workspace = os.getcwd()
transcript = storage / 'projects' / re.sub(r'[^a-zA-Z0-9]', '-', workspace) / (session + '.jsonl')
if flag == '--resume' and not transcript.is_file():
    print('No conversation found with session ID: ' + session, flush=True)
    sys.exit(1)
if (storage / 'fail').exists():
    print('SECRET_api_key_do_not_store\nNo conversation found with session ID: ' + session, flush=True)
    sys.exit(1)


def save():
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with transcript.open('a') as out:
        out.write(json.dumps({'type': 'user', 'sessionId': session, 'cwd': workspace,
                              'message': {'role': 'user', 'content': 'fixture interaction'}}) + '\n')

if '--' in args and args[args.index('--') + 1:]:
    save()
Path(os.environ['MV2_AGENT_DIR'], 'seen.json').write_text(json.dumps({
    'argv': args, 'home': os.environ['HOME'], 'storage': str(storage), 'cwd': workspace,
    'tty': [os.isatty(fd) for fd in (0, 1, 2)],
    'parent_markers': [key for key in ('CODEX_THREAD_ID', 'CLAUDECODE') if key in os.environ]}))
print('HARNESS READY', flush=True)
for line in sys.stdin:
    save()
    print('SAVED', flush=True)
while True:
    time.sleep(1)
