"""Per-user, cross-process admission for new managed SSH starts only."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import random
import socket
import sqlite3
import subprocess
import threading
import time
import uuid


SSH_COMMON_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1']
SSH_OPTIONS = ['-T', *SSH_COMMON_OPTIONS]
WINDOW = 10.0
STARTS = 3
MAX_WAIT = 45.0
PERMANENT_COOLDOWN = 300.0
STABLE_SESSION = 60.0
CIRCUIT_FAILURES = 3


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Target:
    endpoints: tuple[str, ...]
    credential: str
    trust: str
    route: str

    def failure_keys(self, kind):
        context = {'network': '', 'authentication_diagnostic': self.credential,
                   'host_key_diagnostic': self.trust,
                   'unknown_transport': self.credential + self.route,
                   'remote_exit': self.credential + self.route,
                   'spawn_error': self.route}[kind]
        return tuple(digest([kind, key, context]) for key in self.endpoints)

    def all_failure_keys(self):
        return tuple(key for kind in ('network', 'authentication_diagnostic', 'host_key_diagnostic',
                     'unknown_transport', 'remote_exit', 'spawn_error') for key in self.failure_keys(kind))


def _metadata(names):
    result = []
    for name in names:
        path = Path(name).expanduser()
        try:
            stat = path.stat()
            result.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
        except OSError:
            result.append((str(path), None))
    return result  # Only hashed by caller; contents are never opened.


def target_from_config(text, addresses, command):
    values = {}
    for line in text.splitlines():
        key, _, value = line.partition(' ')
        values.setdefault(key.lower(), []).append(value.strip())
    def first(key, default=''):
        return values.get(key, [default])[0]
    hostname = first('hostname').casefold().rstrip('.')
    port = int(first('port'))
    if not hostname or not first('user') or not 1 <= port <= 65535:
        raise ValueError('effective SSH endpoint is incomplete')
    if any(first(key, 'none') not in ('', 'none') for key in ('proxycommand', 'proxyjump')):
        raise ValueError('startup admission supports declared direct endpoints, not proxy routes')
    hosts = {'host:' + hostname}
    for address in addresses:
        hosts.add('ip:' + ipaddress.ip_address(address).compressed.casefold())
    if len(hosts) == 1:
        raise ValueError('effective SSH endpoint has no verified addresses')
    endpoints = tuple(sorted(digest([host, port]) for host in hosts))
    credential = digest([first('user'), values.get('identityfile', []), values.get('certificatefile', []),
        _metadata(values.get('identityfile', []) + values.get('certificatefile', [])),
        first('identityagent'), first('identitiesonly'), first('preferredauthentications')])
    trust_name = first('hostkeyalias')
    trust = digest([hostname if trust_name in ('', 'none') else trust_name, first('stricthostkeychecking'),
                    _metadata(values.get('userknownhostsfile', []) + values.get('globalknownhostsfile', []))])
    return Target(endpoints, credential, trust, digest(command))


def resolve_target(alias, command, *, ssh_prefix=None, options=None):
    if not isinstance(alias, str) or not alias or alias.startswith('-') or any(c.isspace() or c == '\0' for c in alias):
        raise ValueError('invalid SSH alias')
    result = subprocess.run([*(ssh_prefix or ['ssh']), '-G', *(SSH_OPTIONS if options is None else options), alias], capture_output=True,
                            encoding='utf-8', errors='strict', timeout=5, check=False)
    if result.returncode:
        raise RuntimeError('effective SSH configuration evaluation failed; transport not started')
    config = dict(line.split(' ', 1) for line in result.stdout.splitlines() if ' ' in line)
    hostname, port = config.get('hostname'), config.get('port')
    resolved = []
    errors = []
    def lookup():
        try:
            for item in socket.getaddrinfo(hostname, int(port), type=socket.SOCK_STREAM):
                address = item[4][0]
                if len(item[4]) > 3 and item[4][3] and '%' not in address:
                    address += '%' + str(item[4][3])
                resolved.append(address)
        except (OSError, ValueError, TypeError):
            errors.append(True)
    thread = threading.Thread(target=lookup, daemon=True)
    thread.start(); thread.join(5)
    if thread.is_alive() or errors:
        raise RuntimeError('effective SSH address resolution unavailable; transport not started')
    return target_from_config(result.stdout, resolved, command)


def state_path():
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA')
        if not base:
            raise RuntimeError('LOCALAPPDATA is required for shared startup admission')
        root = Path(base) / 'Codex' / 'solution-agent'
    else:
        root = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state'))) / 'codex/solution-agent'
    return root / 'ssh-start-budget-v1' / 'admission.sqlite'


class AdmissionError(RuntimeError):
    pass


class Budget:
    def __init__(self, path, *, clock=time.time, monotonic=time.monotonic, sleep=time.sleep, jitter=random.random):
        self.path = Path(path)
        self.clock, self.monotonic, self.sleep, self.jitter = clock, monotonic, sleep, jitter

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            self.path.parent.chmod(0o700)
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''CREATE TABLE IF NOT EXISTS attempts (
            endpoint TEXT NOT NULL, token TEXT NOT NULL, started REAL NOT NULL, outcome TEXT NOT NULL,
            PRIMARY KEY(endpoint,token));
            CREATE TABLE IF NOT EXISTS failures (
            key TEXT PRIMARY KEY, count INTEGER NOT NULL, last REAL NOT NULL, until REAL NOT NULL,
            kind TEXT NOT NULL, attempt_order INTEGER NOT NULL);''')
            if os.name != 'nt':
                self.path.chmod(0o600)
            with db:
                yield db
        finally:
            db.close()

    def admit(self, target, *, max_wait=MAX_WAIT, notify=lambda message: None):
        deadline = self.monotonic() + max_wait
        notified = False
        while True:
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                now = self.clock()
                db.execute('DELETE FROM attempts WHERE started < ?', (now - 900,))
                db.execute('DELETE FROM failures WHERE until < ? AND last < ?', (now, now - 300))
                # A backwards clock jump must not cause a permanent future lock.
                db.execute('UPDATE attempts SET started=? WHERE started>?', (now, now))
                db.execute('''UPDATE failures SET last=?,until=?+CASE
                    WHEN kind IN ('authentication_diagnostic','host_key_diagnostic')
                    OR (kind IN ('network','unknown_transport') AND count>=3) THEN 300 ELSE 31 END
                    WHERE last>? OR until>?''', (now, now, now, now + PERMANENT_COOLDOWN))
                delay, reason, terminal = 0.0, 'endpoint start-rate budget', False
                for key in target.all_failure_keys():
                    row = db.execute('SELECT until,kind FROM failures WHERE key=?', (key,)).fetchone()
                    if row and row[0] > now:
                        delay = max(delay, row[0] - now)
                        reason = row[1] + ' cooldown'
                        terminal |= row[1] in ('authentication_diagnostic', 'host_key_diagnostic') or row[0] - now > 31
                for key in target.endpoints:
                    rows = db.execute('SELECT started FROM attempts WHERE endpoint=? AND started>? ORDER BY started',
                                      (key, now - WINDOW)).fetchall()
                    if len(rows) >= STARTS:
                        delay = max(delay, rows[-STARTS][0] + WINDOW - now)
                if delay <= 0:
                    token = uuid.uuid4().hex
                    db.executemany('INSERT INTO attempts VALUES (?,?,?,?)',
                                   [(key, token, now, 'admitted') for key in target.endpoints])
                    return token, now
            if terminal:
                raise AdmissionError(f'{reason}; stop without retry, retry-after={delay:.1f}s; inspect configuration')
            remaining = deadline - self.monotonic()
            if delay > remaining or remaining <= 0:
                raise AdmissionError(f'{reason}; bounded admission wait exhausted, retry-after={delay:.1f}s')
            if not notified:
                notify(f'Harness startup waiting: {reason}; no transport started yet')
                notified = True
            self.sleep(max(0.01, delay))  # Never hold SQLite while waiting.

    def finish(self, target, token, started, kind):
        now = self.clock()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            attempt = db.execute('SELECT max(rowid),outcome FROM attempts WHERE token=?', (token,)).fetchone()
            if attempt[0] is None or attempt[1] != 'admitted':
                return
            order = attempt[0]
            db.execute('UPDATE attempts SET outcome=? WHERE token=?', (kind, token))
            if kind == 'success':
                for key in target.all_failure_keys():
                    db.execute('DELETE FROM failures WHERE key=? AND attempt_order<?', (key, order))
                return
            if now - started >= STABLE_SESSION:
                return  # Established-session closure is not a startup retry burst.
            for key in target.failure_keys(kind):
                row = db.execute('SELECT count,last,attempt_order FROM failures WHERE key=?', (key,)).fetchone()
                count = min(10, row[0] + 1) if row and now - row[1] < 300 else 1
                delay = (PERMANENT_COOLDOWN if kind in ('authentication_diagnostic','host_key_diagnostic')
                         or (kind in ('network','unknown_transport') and count >= CIRCUIT_FAILURES)
                         else min(30.0, 2.0 ** count) + min(1.0, max(0.0, self.jitter())))
                db.execute('INSERT OR REPLACE INTO failures VALUES (?,?,?,?,?,?)',
                           (key,count,now,now+delay,kind,max(order,row[2] if row else order)))


def classify(code, stderr):
    if code == 0:
        return 'success'
    if code != 255:
        return 'remote_exit'
    text = stderr.decode('utf-8', errors='replace').casefold()
    if 'host key verification failed' in text or 'remote host identification has changed' in text:
        return 'host_key_diagnostic'
    if 'permission denied (' in text or 'too many authentication failures' in text:
        return 'authentication_diagnostic'
    if any(s in text for s in ('connection timed out','connection refused','connection reset',
                               'connection closed by','no route to host','network is unreachable')):
        return 'network'
    return 'unknown_transport'


def run_transport(argv, *, stdin, stdout, stderr):
    """Leave MCP stdin/stdout independent; tee only stderr with bounded RAM."""
    process = subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE)
    tail = bytearray()
    lock = threading.Lock()
    def pump():
        try:
            while True:
                block = process.stderr.read1(4096)
                if not block:
                    break
                with lock:
                    tail.extend(block)
                    del tail[:-16384]
                try:
                    stderr.write(block); stderr.flush()
                except (OSError, ValueError):
                    pass  # Continue draining even if the diagnostic consumer closes.
        except (OSError, ValueError):
            pass
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    code = process.wait()
    reader.join(2)
    with lock:
        return code, bytes(tail)


def _bytes(value):
    return value if isinstance(value, bytes) else (value or '').encode('utf-8', errors='replace')


def begin(alias, command, *, ssh_prefix=None, options=None):
    target = resolve_target(alias, command, ssh_prefix=ssh_prefix, options=options)
    gate = Budget(state_path())
    token, started = gate.admit(target)
    return gate, target, token, started


def guarded_run(alias, command, invoke, *, ssh_prefix=None, options=None):
    gate, target, token, started = begin(alias, command, ssh_prefix=ssh_prefix, options=options)
    try:
        result = invoke()
    except subprocess.TimeoutExpired:
        gate.finish(target, token, started, 'network')
        raise
    except OSError:
        gate.finish(target, token, started, 'spawn_error')
        raise
    gate.finish(target, token, started, classify(result.returncode, _bytes(result.stderr)))
    return result


def guarded_start(alias, command, invoke, *, ssh_prefix=None, options=None):
    gate, target, token, started = begin(alias, command, ssh_prefix=ssh_prefix, options=options)
    try:
        process = invoke()
    except OSError:
        gate.finish(target, token, started, 'spawn_error')
        raise
    def completed():
        # The existing diagnostic stream combines stderr/stdout. Do not consume
        # it or infer authentication from remote output; preserve caller ownership.
        try:
            gate.finish(target, token, started, classify(process.wait(), b''))
        except (OSError, sqlite3.Error):
            import sys
            print('SSH startup outcome unavailable; admission remains reserved until expiry', file=sys.stderr)
    threading.Thread(target=completed, daemon=True).start()
    return process
