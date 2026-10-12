from pathlib import Path

import pytest

from solution_agent.harness.protocol import ActivationGate, EndpointIdentity, ProtocolMismatch


def _identity(release="r1", payload="p1"):
    return EndpointIdentity("0.6", release, payload, "schema")


def test_activation_requires_matching_two_ended_identity():
    gate = ActivationGate()
    with pytest.raises(ProtocolMismatch):
        gate.activate(_identity(), _identity(payload="changed"), 1)
    assert not gate.active
    gate.activate(_identity(), _identity(), 7)
    gate.require(7)
    with pytest.raises(ProtocolMismatch):
        gate.require(6)


def test_activation_invalidates_on_generation_reset():
    gate = ActivationGate()
    gate.activate(_identity(), _identity(), 1)
    gate.invalidate()
    with pytest.raises(ProtocolMismatch):
        gate.require()
