"""Explicit stdio acceptance through the existing release-owned SSH launcher.

Does not install plugins or alter bindings. All control inputs are supplied by
the caller. The command prints cwd; optional synthetic artifact acceptance uses
the existing diff-review/CAS tools against an explicit new test target.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp import Client, StdioServerParameters
from solution_agent.harness.protocol import identity_from_manifest, verify_pair
from solution_agent.harness.release_identity import CANONICAL_TOOL_NAMES


async def check(args):
    manifest = json.loads((args.plugin / 'release_manifest.json').read_text(encoding='utf-8'))
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'USERPROFILE', 'HOME',
        'TEMP', 'TMP', 'SYSTEMDRIVE', 'PATHEXT',
        'USERNAME', 'USERDOMAIN', 'USERDOMAIN_ROAMINGPROFILE', 'HOMEDRIVE',
        'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'ALLUSERSPROFILE',
        'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432',
        'OS', 'PROCESSOR_ARCHITECTURE', 'LANG', 'LC_ALL', 'LC_CTYPE',
    }}
    environment['PYTHONUNBUFFERED'] = '1'
    parameters = StdioServerParameters(
        command=str(args.python),
        args=[str(args.plugin / 'scripts/remote_launcher.py'),
              '--control-root', str(args.control_root), '--profile', args.profile],
        cwd=str(args.control_root), env=environment,
    )
    if args.namespace_config:
        # Reuse the release-owned route and its exact protocol/workplace env.
        # Only the explicit acceptance transport wraps it in an isolated FS.
        script = args.plugin / 'scripts/remote_launcher.py'
        spec = importlib.util.spec_from_file_location('acceptance_route', script)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        alias, remote_command = launcher.route(args.control_root, args.profile, args.plugin)
        pieces = shlex.split(remote_command)
        split = next(i for i, piece in enumerate(pieces[1:], 1) if '=' not in piece)
        identity_env = [piece for piece in pieces[1:split] if piece.split('=', 1)[0] in {
            'SOLUTION_AGENT_PROTOCOL_REQUIRED', 'SOLUTION_AGENT_EXPECTED_IDENTITY_B64',
            'SOLUTION_AGENT_WORKPLACE_IDENTITY_B64'}]
        wrapped = ['env', *identity_env, args.remote_python, args.namespace_helper,
                                   '--config', args.namespace_config, '--'] + pieces[split:]
        parameters = StdioServerParameters(
            command='ssh', args=['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 alias, shlex.join(wrapped)],
            cwd=str(args.control_root), env=environment)
    # Harness retains its initialize-era MCP surface. Select that documented
    # SDK handshake explicitly; Harness protocol 0.6 identity checks are separate.
    async with Client(parameters, read_timeout_seconds=60, mode='legacy') as client:
        listed = await client.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        if names != sorted(CANONICAL_TOOL_NAMES):
            raise RuntimeError('Remote tool surface differs from the canonical eleven tools')
        arguments = {'task_id': args.task_id, 'access_mode': 'read_only',
                     'wait_ms': 1000, 'max_preview_bytes': 1000}
        if args.command_id:
            arguments['command_id'] = args.command_id
        else:
            arguments.update(command="python3 -c 'import json,os; print(json.dumps({\"cwd\":os.getcwd(),\"marker\":\"agent-relay-acceptance\"}))'",
                             timeout_seconds=30)
        called = await client.call_tool('terminal_exec', arguments)
        result = called.structured_content
        if not result or not result.get('ok'):
            raise RuntimeError(f'Remote call failed: {result or called.content}')
        command_id = result['command_id']
        # Resume the original ID only; never resubmit an uncertain command.
        for _ in range(6):
            if result.get('state') not in {'queued', 'starting', 'running'}:
                break
            called = await client.call_tool('terminal_exec', {
                'task_id': args.task_id, 'command_id': command_id,
                'wait_ms': 5000, 'max_preview_bytes': 1000,
            })
            result = called.structured_content
            if not result or not result.get('ok'):
                raise RuntimeError('Original command lookup failed')
        if result.get('state') != 'succeeded' or result.get('exit_code') != 0:
            raise RuntimeError(f'Probe did not succeed: {result}')
        verify_pair(identity_from_manifest(manifest), identity_from_manifest({
            'protocol_version': manifest['protocol_version'], **result['server'],
        }))
        stdout = result['stdout']
        if stdout.get('truncated'):
            raise RuntimeError('Acceptance stdout unexpectedly truncated')
        preview = stdout['preview']
        if hashlib.sha256(preview.encode()).hexdigest() != stdout['sha256']:
            raise RuntimeError('Result preview does not match its receipt hash')
        decoded = json.loads(preview)
        if decoded != {'cwd': args.expected_cwd, 'marker': 'agent-relay-acceptance'}:
            raise RuntimeError(f'Wrong workplace execution target: {decoded}')
        recovered = await client.call_tool('terminal_exec', {
            'task_id': args.task_id, 'command_id': command_id,
            'wait_ms': 0, 'max_preview_bytes': 1000,
        })
        replay = recovered.structured_content
        if replay['command_id'] != command_id or replay['stdout']['sha256'] != stdout['sha256']:
            raise RuntimeError('Original command recovery changed result identity')
        artifact_receipt = None
        if args.artifact_source:
            async def call(name, values):
                response = await client.call_tool(name, {'task_id': args.task_id, **values})
                data = response.structured_content
                if not data or not data.get('ok'):
                    raise RuntimeError(f'{name} acceptance failed: {data or response.content}')
                return data
            stored = await call('artifact_store', {'path': args.artifact_source})
            artifact = stored['manifest']
            expected_bytes = b'agent-relay-artifact-acceptance\n'
            expected_hash = hashlib.sha256(expected_bytes).hexdigest()
            if artifact['sha256'] != expected_hash:
                raise RuntimeError('Artifact source is not the explicit synthetic acceptance fixture')
            planned = await call('artifact_apply', {'mode': 'materialize', 'request': {
                'target': args.artifact_target, 'artifact_id': artifact['artifact_id'],
                'expected_target_sha256': None}})
            plan = planned['plan']
            reviewed = await call('artifact_inspect', {'artifact_id': plan['diff_artifact_id'],
                'mode': 'read', 'selector': {'type': 'whole'}, 'max_output_bytes': 4096})
            if reviewed.get('truncated') or '+agent-relay-artifact-acceptance' not in reviewed.get('content', ''):
                raise RuntimeError('Artifact apply diff does not match the synthetic fixture')
            committed = await call('artifact_apply', {'mode': 'materialize', 'commit': True,
                'plan_id': plan['plan_id'], 'writer_lease_id': planned['writer_lease_id']})
            artifact_receipt = committed['receipt']
            if artifact_receipt['after_sha256'] != expected_hash:
                raise RuntimeError('Committed artifact hash differs from reviewed source')
            target = await call('artifact_store', {'path': args.artifact_target})
            if target['manifest']['sha256'] != expected_hash:
                raise RuntimeError('Materialized target does not match its receipt')
        report = {'status': 'PASSED', 'scope': 'EXPLICIT_WINDOWS_LINUX_STDIO_COMPONENT_ACCEPTANCE',
                  'profile': args.profile, 'canonical_tools': names,
                  'server': result['server'], 'command_id': command_id,
                  'execution_release': result['runtime_release_id'],
                  'stdout_sha256': stdout['sha256'], 'cwd': decoded['cwd'],
                  'original_id_recovered_without_resubmit': True}
        if args.namespace_config:
            report['isolation'] = 'LINUX_NAMESPACE_PRIVATE_SYSTEMD'
        if artifact_receipt:
            report['artifact_roundtrip'] = artifact_receipt
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--plugin', type=Path, required=True)
    parser.add_argument('--control-root', type=Path, required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--expected-cwd', required=True)
    parser.add_argument('--command-id')
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--namespace-config', help='Explicit remote private acceptance config')
    parser.add_argument('--namespace-helper', help='Explicit remote namespace acceptance helper')
    parser.add_argument('--remote-python', help='Explicit remote isolated Python with YAML support')
    parser.add_argument('--artifact-source', help='Remote synthetic fixture with fixed acceptance content')
    parser.add_argument('--artifact-target', help='Explicit new remote test target; create-only CAS')
    args = parser.parse_args()
    if any((args.namespace_config, args.namespace_helper, args.remote_python)) and not all((args.namespace_config, args.namespace_helper, args.remote_python)):
        parser.error('Namespace acceptance needs config, helper and remote Python together')
    if bool(args.artifact_source) != bool(args.artifact_target):
        parser.error('Artifact acceptance needs source and a new target together')
    args.python = args.python.resolve()
    args.plugin = args.plugin.resolve()
    args.control_root = args.control_root.resolve()
    args.receipt = args.receipt.resolve()
    if (ROOT / '.verification').resolve() not in args.receipt.parents:
        parser.error('Private acceptance receipts must stay below repo .verification')
    if args.receipt.exists():
        parser.error('Preserve previous receipts; choose a new output path')
    asyncio.run(check(args))


if __name__ == '__main__':
    main()
