"""Run an unchanged Harness release at its original paths in a private Linux namespace.

Acceptance helper only; not a deployment launcher. Private JSON config supplies
sandbox_root, release_root, python_root, venv_root, workplace_root, run_root,
runtime_root, state_root and profile_id. All mounted inputs must belong to sandbox_root.
No host HOME, SSH credentials, D-Bus socket or research directory is exposed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time
import uuid


def config(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    base = Path(value['sandbox_root']).resolve(strict=True)
    for key in ('release_root', 'python_root', 'venv_root', 'workplace_root', 'run_root', 'runtime_root', 'state_root'):
        selected = Path(value[key]).resolve(strict=True)
        if base not in selected.parents:
            raise ValueError(f'{key} must be strictly below sandbox_root')
        value[key] = str(selected)
    import yaml
    profile = yaml.safe_load((Path(value['release_root']) / 'config/harness_remote_deploy_profiles.yaml').read_text())['profiles'][value['profile_id']]
    value['original_release_root'] = profile['solution_agent_root']
    value['original_run_root'] = profile['remote_run_root']
    value['original_workplace_root'] = profile['remote_terminals']['slots'][profile['remote_terminals']['default']]['cwd']
    for key in ('original_release_root', 'original_run_root', 'original_workplace_root'):
        p = PurePosixPath(value[key])
        if not p.is_absolute() or p == PurePosixPath('/') or '..' in p.parts:
            raise ValueError(f'Unsafe original path: {key}')
    # Prevent a later bind from hiding one of the primary mounts.
    primary = [PurePosixPath(value[k]) for k in ('original_release_root', 'original_run_root', 'original_workplace_root')]
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(primary) for b in primary[i + 1:]):
        raise ValueError('Original release, run and workplace paths must be disjoint')
    mountpoint = Path(value['release_root']) / 'state'
    if mountpoint.is_symlink():
        raise ValueError('Release state mountpoint must not be a symlink')
    mountpoint.mkdir(exist_ok=True)
    return value


def inside(command):
    # Let the private manager's dbus.socket own the user bus. A separate
    # dbus-run-session bus cannot activate this manager's transient units.
    os.environ.pop('DBUS_SESSION_BUS_ADDRESS', None)
    manager = subprocess.Popen(['/usr/lib/systemd/systemd', '--user'], stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
    try:
        for _ in range(100):
            if manager.poll() is not None:
                raise RuntimeError(f'Private systemd manager exited: {manager.returncode}')
            checked = subprocess.run(['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if checked.returncode == 0:
                break
            time.sleep(.05)
        else:
            raise RuntimeError('Private systemd manager did not become ready')
        return subprocess.call(command)
    finally:
        subprocess.run(['systemctl', '--user', 'exit'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        try:
            manager.wait(timeout=5)
        except subprocess.TimeoutExpired:
            manager.terminate()
            manager.wait(timeout=5)


def delegated(value, command):
    relative = Path('/proc/self/cgroup').read_text().strip().split(':')[-1]
    group = Path('/sys/fs/cgroup' + relative)
    if not os.access(group, os.W_OK) or group.stat().st_uid != os.getuid():
        raise RuntimeError('A private delegated cgroup owned by the current user is required')
    runtime = Path(value['runtime_root'])
    runtime.chmod(0o700)
    machine = runtime / 'machine-id'
    if not machine.exists():
        machine.write_text(uuid.uuid4().hex + '\n', encoding='ascii')
    args = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/lib64', '/lib64', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/sbin', '/sbin', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--dir', '/etc', '--dir', '/run/systemd/system',
            '--ro-bind', '/etc/passwd', '/etc/passwd', '--ro-bind', '/etc/group', '/etc/group',
            '--ro-bind', str(machine), '/etc/machine-id',
            '--bind', str(group), '/sys/fs/cgroup',
            '--ro-bind', value['release_root'], value['original_release_root'],
            '--bind', value['workplace_root'], value['original_workplace_root'],
            '--bind', value['run_root'], value['original_run_root'],
            '--bind', value['state_root'], value['original_release_root'] + '/state',
            '--bind', str(runtime), f'/run/user/{os.getuid()}',
            '--ro-bind', value['python_root'], value['python_root'],
            '--ro-bind', value['venv_root'], value['venv_root'],
            '--ro-bind', str(Path(__file__).resolve()), '/runner/helper.py',
            '--clearenv']
    env = {'HOME': str(Path(value['original_release_root']).parents[2]),
           'PATH': value['venv_root'] + '/bin:/usr/bin:/bin',
           'PYTHONPATH': value['original_release_root'],
           'SOLUTION_AGENT_ROOT': value['original_release_root'],
           'SOLUTION_AGENT_PROFILE_ID': value['profile_id'],
           'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}', 'container': 'bwrap',
           'LANG': 'C.UTF-8'}
    for key in ('SOLUTION_AGENT_PROTOCOL_REQUIRED', 'SOLUTION_AGENT_EXPECTED_IDENTITY_B64', 'SOLUTION_AGENT_WORKPLACE_IDENTITY_B64'):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, val in env.items():
        args += ['--setenv', key, val]
    args += ['--chdir', value['original_workplace_root'], '--', value['venv_root'] + '/bin/python',
             '/runner/helper.py', '--inside', '--', *command]
    return subprocess.call(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--delegated', action='store_true')
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Linux acceptance helper only')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('An explicit command is required')
    if args.inside:
        return inside(command)
    if args.config is None:
        parser.error('--config is required')
    value = config(args.config.resolve())
    if args.delegated:
        return delegated(value, command)
    # A transient user unit delegates only its own cgroup and owns cleanup.
    launch = ['systemd-run', '--user', '--wait', '--pipe', '--collect',
              '--quiet', '--unit=agent-relay-acceptance-' + uuid.uuid4().hex,
              '--property=Delegate=yes']
    for key in ('SOLUTION_AGENT_PROTOCOL_REQUIRED', 'SOLUTION_AGENT_EXPECTED_IDENTITY_B64', 'SOLUTION_AGENT_WORKPLACE_IDENTITY_B64'):
        if key in os.environ:
            launch.append('--setenv=' + key + '=' + os.environ[key])
    return subprocess.call(launch + [sys.executable, str(Path(__file__).resolve()),
                                    '--config', str(args.config.resolve()), '--delegated', '--', *command])


if __name__ == '__main__':
    raise SystemExit(main())
