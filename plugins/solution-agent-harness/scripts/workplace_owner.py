"""Shared local transport lease and process containment; never reconnects or dispatches."""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

LEASE_ENV = 'SOLUTION_AGENT_OWNER_LEASE'


def workplace_key(identity: Path) -> str:
    value = identity.read_bytes()
    if not re.fullmatch(rb'solution-agent-workplace-v1:[a-f0-9]{32}\n', value):
        raise RuntimeError('owner_workplace_identity_invalid')
    return hashlib.sha256(value).hexdigest()


def state_root() -> Path:
    base = (Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) if os.name == 'nt'
            else Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')))
    value = os.path.abspath(base / 'AgentRelay' / 'transport-owners')
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)


def kernel():
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, args, result in [
        ('CreateJobObjectW', [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        ('OpenJobObjectW', [w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        ('OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL),
        ('IsProcessInJob', [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        ('SetInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        ('QueryInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
        ('TerminateJobObject', [w.HANDLE, w.UINT], w.BOOL),
        ('GetProcessTimes', [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        ('GetExitCodeProcess', [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        ('CloseHandle', [w.HANDLE], w.BOOL),
    ]:
        fn = getattr(k, name); fn.argtypes = args; fn.restype = result
    return k


def process_marker(pid: int) -> str | None:
    if os.name == 'nt':
        from ctypes import wintypes as w
        k = kernel(); h = k.OpenProcess(0x1000, False, pid)
        if not h:
            if ctypes.get_last_error() == 87:
                return None
            raise RuntimeError('owner_process_identity_unknown')
        try:
            fields = [w.FILETIME() for _ in range(4)]; code = w.DWORD()
            if not k.GetProcessTimes(h, *(ctypes.byref(v) for v in fields)) or not k.GetExitCodeProcess(h, ctypes.byref(code)):
                raise RuntimeError('owner_process_identity_unknown')
            return str((fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime) if code.value == 259 else None
        finally:
            k.CloseHandle(h)
    try:
        # The comm field can contain spaces and parentheses; split after its final ')'.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except FileNotFoundError:
        return None


def job_name(key: str) -> str:
    return 'Global\\AgentRelay.WorkplaceOwner.' + key


class WindowsTree:
    def __init__(self, key: str):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('limit', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('schedule', w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_ulonglong * 6),
                        ('memory', ctypes.c_size_t * 4)]
        self.k = kernel(); self.h = self.k.CreateJobObjectW(None, job_name(key))
        if not self.h:
            raise RuntimeError('owner_job_create_failed')
        try:
            if self.active():
                raise RuntimeError('owner_previous_tree_still_active')
            limits = Extended(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
            if not self.k.SetInformationJobObject(self.h, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise RuntimeError('owner_job_limits_failed')
        except BaseException:
            self.close(); raise

    def active(self) -> int:
        from ctypes import wintypes as w
        class Accounting(ctypes.Structure):
            _fields_ = [('times', ctypes.c_longlong * 4), ('faults', w.DWORD),
                        ('total', w.DWORD), ('active', w.DWORD), ('terminated', w.DWORD)]
        info = Accounting()
        if not self.k.QueryInformationJobObject(self.h, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise RuntimeError('owner_job_query_failed')
        return info.active

    def adopt_and_resume(self, pid: int):
        from ctypes import wintypes as w
        k = self.k; h = k.OpenProcess(0x0100 | 0x0001, False, pid)
        if not h:
            raise RuntimeError('owner_child_open_failed')
        try:
            if not k.AssignProcessToJobObject(self.h, h):
                raise RuntimeError('owner_child_containment_failed')
        finally:
            k.CloseHandle(h)
        class Thread(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('tid', w.DWORD),
                        ('pid', w.DWORD), ('priority', w.LONG), ('delta', w.LONG), ('flags', w.DWORD)]
        k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]; k.CreateToolhelp32Snapshot.restype = w.HANDLE
        k.Thread32First.argtypes = k.Thread32Next.argtypes = [w.HANDLE, ctypes.POINTER(Thread)]
        k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]; k.OpenThread.restype = w.HANDLE
        k.ResumeThread.argtypes = [w.HANDLE]; k.ResumeThread.restype = w.DWORD
        snap = k.CreateToolhelp32Snapshot(4, 0)
        if snap == ctypes.c_void_p(-1).value:
            raise RuntimeError('owner_child_threads_unknown')
        try:
            entry = Thread(); entry.size = ctypes.sizeof(entry); more = k.Thread32First(snap, ctypes.byref(entry))
            while more:
                if entry.pid == pid:
                    thread = k.OpenThread(2, False, entry.tid)
                    if not thread:
                        raise RuntimeError('owner_child_resume_failed')
                    try:
                        if k.ResumeThread(thread) in (0, 0xffffffff):
                            raise RuntimeError('owner_child_resume_failed')
                        return
                    finally:
                        k.CloseHandle(thread)
                more = k.Thread32Next(snap, ctypes.byref(entry))
            raise RuntimeError('owner_child_threads_unknown')
        finally:
            k.CloseHandle(snap)

    def finish(self):
        if not self.k.TerminateJobObject(self.h, 1):
            raise RuntimeError('owner_child_cleanup_failed')
        end = time.monotonic() + 5
        while self.active():
            if time.monotonic() >= end:
                raise RuntimeError('owner_child_cleanup_unconfirmed')
            time.sleep(.02)

    def close(self):
        if self.h:
            self.k.CloseHandle(self.h); self.h = None


def validate_inherited(identity: Path) -> bool:
    raw = os.environ.get(LEASE_ENV)
    if raw is None:
        return False
    try:
        token = json.loads(raw); key = workplace_key(identity)
        record = json.loads((state_root() / f'{key}.json').read_text(encoding='utf8'))
        if record.get('schema') != 'workplace-transport-owner/v1' or record.get('key') != key:
            raise ValueError('schema')
        if token.get('key') != key or not secrets.compare_digest(str(token.get('nonce', '')), record['nonce']):
            raise ValueError('identity')
        for field in ('guardian', 'root'):
            if not isinstance(record[field]['marker'], str) or not record[field]['marker']:
                raise ValueError('identity')
            if process_marker(record[field]['pid']) != record[field]['marker']:
                raise ValueError('stale')
        if os.name == 'nt':
            from ctypes import wintypes as w
            k = kernel(); job = k.OpenJobObjectW(4, False, job_name(key)); proc = k.OpenProcess(0x1000, False, os.getpid())
            try:
                inside = w.BOOL()
                if not job or not proc or not k.IsProcessInJob(proc, job, ctypes.byref(inside)) or not inside.value:
                    raise ValueError('containment')
            finally:
                if job: k.CloseHandle(job)
                if proc: k.CloseHandle(proc)
        elif os.getpgrp() != record['root']['pid']:
            raise ValueError('containment')
        return True
    except (ValueError, KeyError, OSError, TypeError):
        raise RuntimeError('owner_inherited_lease_invalid') from None


def verify_transport_idle(identity: Path) -> None:
    """Read-only proof: the persistent host and this verifier are alone in its tree."""
    if not validate_inherited(identity):
        raise RuntimeError('owner_guardian_required')
    key = workplace_key(identity)
    record = json.loads((state_root() / f'{key}.json').read_text(encoding='utf8'))
    if os.name == 'nt':
        from ctypes import wintypes as w
        k = kernel(); job = k.OpenJobObjectW(4, False, job_name(key))
        if not job:
            raise RuntimeError('owner_job_query_failed')
        try:
            class ProcessInfo(ctypes.Structure):
                _fields_ = [('exit', ctypes.c_void_p), ('peb', ctypes.c_void_p), ('affinity', ctypes.c_void_p),
                            ('priority', ctypes.c_void_p), ('pid', ctypes.c_size_t), ('parent', ctypes.c_size_t)]
            query = ctypes.WinDLL('ntdll').NtQueryInformationProcess
            query.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.c_void_p]
            query.restype = ctypes.c_long
            allowed = set(); pid = os.getpid()
            # Windows venv Python can introduce redirector processes. Permit only
            # this verifier's exact ancestor chain back to the pinned owner root.
            for _ in range(8):
                allowed.add(pid)
                if pid == record['root']['pid']:
                    break
                handle = k.OpenProcess(0x1000, False, pid)
                try:
                    info = ProcessInfo()
                    if not handle or query(handle, 0, ctypes.byref(info), ctypes.sizeof(info), None) != 0:
                        raise RuntimeError('owner_idle_verifier_parent_unknown')
                    pid = info.parent
                finally:
                    if handle: k.CloseHandle(handle)
            else:
                raise RuntimeError('owner_idle_verifier_parent_mismatch')
            class Members(ctypes.Structure):
                _fields_ = [('assigned', w.DWORD), ('count', w.DWORD), ('pids', ctypes.c_size_t * 4096)]
            image = k.QueryFullProcessImageNameW
            image.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
            image.restype = w.BOOL
            deadline = time.monotonic() + 5
            while True:
                members = Members()
                if not k.QueryInformationJobObject(job, 3, ctypes.byref(members), ctypes.sizeof(members), None):
                    raise RuntimeError('owner_job_query_failed')
                extra = []
                for remaining in set(members.pids[:members.count]) - allowed:
                    handle = k.OpenProcess(0x1000, False, remaining)
                    try:
                        info = ProcessInfo(); buffer = ctypes.create_unicode_buffer(32768); length = w.DWORD(32768)
                        known = handle and query(handle, 0, ctypes.byref(info), ctypes.sizeof(info), None) == 0 and image(handle, 0, buffer, ctypes.byref(length))
                        # Node's hidden Python verifier allocates a Windows console
                        # host. Only the actual System32 binary directly parented
                        # by the verifier chain is allowed, never a host child,
                        # arbitrary same-name process or its descendants.
                        console = Path(os.environ['SystemRoot']) / 'System32' / 'conhost.exe'
                        if known and info.parent in allowed - {record['root']['pid']} and os.path.normcase(buffer.value) == os.path.normcase(str(console)):
                            continue
                        extra.append({'pid': remaining, 'parent': info.parent if known else None, 'name': Path(buffer.value).name if known else 'unknown'})
                    finally:
                        if handle: k.CloseHandle(handle)
                if members.assigned == members.count and members.count <= 4096 and not extra and allowed <= set(members.pids[:members.count]):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('owner_transport_tree_still_active:' + json.dumps({'members': list(members.pids[:members.count]), 'verifier_chain': sorted(allowed), 'extra': extra}))
                time.sleep(.05)
        finally:
            k.CloseHandle(job)
    else:
        if os.getppid() != record['root']['pid']:
            raise RuntimeError('owner_idle_verifier_parent_mismatch')
        allowed = {os.getpid(), record['root']['pid']}
        for path in Path('/proc').glob('[0-9]*/stat'):
            try:
                fields = path.read_text().rsplit(')', 1)[1].split()
            except FileNotFoundError:
                continue
            if fields[0] != 'Z' and int(fields[2]) == record['root']['pid'] and int(path.parent.name) not in allowed:
                raise RuntimeError('owner_transport_tree_still_active')


def guarded_run(identity: Path, command: list[str], *, cooldown_seconds: float = 60, stdin=None, stdout=None, stderr=None) -> int:
    if not command or not 0 <= cooldown_seconds <= 86400:
        raise ValueError('owner_invalid_start')
    if validate_inherited(identity):
        return subprocess.call(command, stdin=stdin, stdout=stdout, stderr=stderr)
    key = workplace_key(identity); root = state_root(); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or getattr(root, 'is_junction', lambda: False)():
        raise RuntimeError('owner_state_root_unsafe')
    path = root / f'{key}.lock'; record_path = root / f'{key}.json'
    if path.is_symlink() or record_path.is_symlink():
        raise RuntimeError('owner_state_path_unsafe')
    handle = path.open('a+b'); os.chmod(path, 0o600); locked = False; child = None; tree = None
    record = None
    def save():
        temporary = record_path.with_suffix('.tmp-' + secrets.token_hex(8))
        try:
            with temporary.open('x', encoding='utf8', newline='\n') as out:
                os.chmod(temporary, 0o600); json.dump(record, out); out.flush(); os.fsync(out.fileno())
            os.replace(temporary, record_path)
        finally:
            if temporary.exists(): temporary.unlink()
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise RuntimeError('owner_workplace_busy') from None
        previous = json.loads(record_path.read_text(encoding='utf8')) if record_path.exists() else None
        if previous:
            if (previous.get('schema') != 'workplace-transport-owner/v1' or previous.get('key') != key
                    or type(previous.get('attempt_at')) not in (int, float) or not math.isfinite(previous['attempt_at'])):
                raise RuntimeError('owner_state_invalid')
            old = previous.get('root')
            if old and process_marker(old['pid']) == old['marker']:
                raise RuntimeError('owner_previous_tree_still_active')
            if os.name != 'nt' and old:
                try: os.killpg(old['pid'], 0)
                except ProcessLookupError: pass
                else: raise RuntimeError('owner_previous_tree_cleanup_required')
            elapsed = time.time() - previous['attempt_at']
            if elapsed < 0 or elapsed < cooldown_seconds:
                raise RuntimeError('owner_start_cooldown')
        tree = WindowsTree(key) if os.name == 'nt' else None
        record = {'schema': 'workplace-transport-owner/v1', 'key': key, 'nonce': secrets.token_hex(32),
                  'attempt_at': time.time(), 'state': 'starting',
                  'guardian': {'pid': os.getpid(), 'marker': process_marker(os.getpid())}, 'parent_pid': os.getppid(), 'root': None}
        save()
        env = {**os.environ, LEASE_ENV: json.dumps({'key': key, 'nonce': record['nonce']})}
        child = subprocess.Popen(command, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                                 creationflags=0x4 if os.name == 'nt' else 0, start_new_session=os.name != 'nt')
        record['root'] = {'pid': child.pid, 'marker': process_marker(child.pid)}; record['state'] = 'running'; save()
        if tree: tree.adopt_and_resume(child.pid)
        record['root_exit_code'] = child.wait(); record['root_exited_at'] = time.time(); save()
        return record['root_exit_code']
    finally:
        try:
            if child:
                if child.poll() is None:
                    child.kill()
                if tree:
                    tree.finish()
                else:
                    try: os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                child.wait(timeout=5)
            if record:
                record['state'] = 'closed'; record['closed_at'] = time.time(); save()
        finally:
            if tree: tree.close()
            if locked:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--cooldown-seconds', type=float, default=60)
    parser.add_argument('--verify-inherited', action='store_true')
    parser.add_argument('--verify-transport-idle', action='store_true')
    parser.add_argument('--log-file', type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.verify_transport_idle:
        verify_transport_idle(args.identity)
        print(json.dumps({'transport_idle': True})); return 0
    if args.verify_inherited:
        if not validate_inherited(args.identity): raise RuntimeError('owner_guardian_required')
        print(json.dumps({'verified': True, 'workplace_sha256': workplace_key(args.identity)})); return 0
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.log_file:
        # The explicit manual broker has no inherited stdio handles. Keep its
        # guardian/backend diagnostics in the declared private host log.
        if not args.log_file.is_absolute(): raise ValueError('owner_absolute_log_required')
        with args.log_file.open('a', encoding='utf-8') as log:
            print(json.dumps({'event':'guardian_manual_start','pid':os.getpid(),'parent_pid':os.getppid(),'at':time.time()}),file=log,flush=True)
            try:
                code=guarded_run(args.identity,command,cooldown_seconds=args.cooldown_seconds,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                print(json.dumps({'event':'guardian_manual_exit','pid':os.getpid(),'code':code,'at':time.time()}),file=log,flush=True)
                return code
            except BaseException as exc:
                print(json.dumps({'event':'guardian_manual_error','type':type(exc).__name__,'message':str(exc),'at':time.time()}),file=log,flush=True)
                raise
    return guarded_run(args.identity, command, cooldown_seconds=args.cooldown_seconds,
                       stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer)


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print('Harness transport guard: ' + str(exc), file=sys.stderr); raise SystemExit(2)
