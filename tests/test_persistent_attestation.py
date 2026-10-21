"""Managed peer attestation is emitted only after the existing startup gates."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from solution_agent.harness import mcp_server as server
from solution_agent.harness import release_identity


def test_unmanaged_metadata_does_not_invent_attestation(monkeypatch):
    monkeypatch.setattr(server, '_managed_peer_attestation', None)
    monkeypatch.setattr(server, '_solution_root', lambda: Path('.').resolve())
    monkeypatch.setattr(server, 'load_terminal_profile', lambda root: SimpleNamespace(profile_id='fixture'))
    monkeypatch.setattr(server, '_cached_server_identity', lambda *args: {'release_id': 'fixture'})
    assert server._server_identity() == {'release_id': 'fixture'}


@pytest.mark.parametrize('workplace_ok', [True, False])
def test_managed_metadata_requires_protocol_and_workplace(monkeypatch, workplace_ok):
    peer = {'protocol_version': '0.6', 'release_id': 'fixture', 'source_root_sha256': 'a'*64, 'mcp_schema_sha256': 'b'*64}
    monkeypatch.setenv('SOLUTION_AGENT_PROTOCOL_REQUIRED', '1')
    monkeypatch.setenv('SOLUTION_AGENT_EXPECTED_IDENTITY_B64', base64.b64encode(json.dumps(peer).encode()).decode())
    monkeypatch.setattr(server, '_managed_peer_attestation', None)
    monkeypatch.setattr(server, '_solution_root', lambda: Path('.').resolve())
    monkeypatch.setattr(server, 'load_terminal_profile', lambda root: SimpleNamespace(profile_id='fixture'))
    monkeypatch.setattr(release_identity, 'build_runtime_identity', lambda *args: SimpleNamespace(compact=lambda: peer))
    monkeypatch.setattr(server, '_activation_gate', server.ActivationGate())
    monkeypatch.setattr(server.mcp, 'run', lambda **kwargs: None)
    def workplace(profile):
        if not workplace_ok:
            raise RuntimeError('workplace rejected')
        return {'sha256': 'c'*64}
    monkeypatch.setattr(server, 'verify_workplace_identity', workplace)
    if workplace_ok:
        server.main()
        assert server._managed_peer_attestation == {'protocol_version': '0.6', 'workplace_sha256': 'c'*64}
    else:
        with pytest.raises(RuntimeError, match='workplace rejected'):
            server.main()
        assert server._managed_peer_attestation is None
