"""Local, narrowly scoped CUPS operations for the unprivileged web worker."""
import grp
import json
import os
import pwd
import re
import socket
import struct
import subprocess
from pathlib import Path

queue = os.environ.get('CUPS_QUEUE', 'printer')
if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', queue):
    raise SystemExit('Invalid CUPS_QUEUE')
app_uid = pwd.getpwnam('appuser').pw_uid
path = Path('/data/cups/state/control.sock')
path.unlink(missing_ok=True)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(path))
os.chown(path, 0, grp.getgrnam('lp').gr_gid)
os.chmod(path, 0o660)
server.listen(8)
while True:
    conn, _ = server.accept()
    with conn:
        try:
            _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != app_uid:
                raise ValueError('Unauthorized client')
            conn.settimeout(3)
            data = b''
            while b'\n' not in data and len(data) < 2048:
                chunk = conn.recv(2048 - len(data))
                if not chunk: break
                data += chunk
            if not data.endswith(b'\n'): raise ValueError('Invalid request')
            request = json.loads(data)
            if not isinstance(request, dict): raise ValueError('Invalid request')
            action, job = request.get('action'), request.get('job')
            if action == 'pause': command = ['cupsdisable', queue]
            elif action == 'resume': command = ['cupsenable', queue]
            elif action in ('cancel', 'retry') and isinstance(job, str) and re.fullmatch(re.escape(queue) + r'-[0-9]+', job):
                command = ['cancel', job] if action == 'cancel' else ['lp', '-i', job, '-H', 'restart']
            else: raise ValueError('Invalid operation')
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            reply = {'ok': result.returncode == 0, 'error': result.stderr.strip()[:300]}
        except (ValueError, TypeError, OSError, struct.error, subprocess.TimeoutExpired):
            reply = {'ok': False, 'error': 'Print control request failed.'}
        try: conn.sendall(json.dumps(reply).encode())
        except OSError: pass
