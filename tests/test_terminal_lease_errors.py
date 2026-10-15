"""HF-014: OS lock faults must not masquerade as a competing writer."""
import errno
import importlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solution_agent.harness.remote_terminal import ProfileTerminalManager, TerminalError, _Lease
from test_remote_terminal import FakeTmux, _profile
import solution_agent.harness.remote_terminal as terminal


@pytest.mark.parametrize("number", [errno.EIO, errno.EBADF, errno.ENOLCK, errno.EINVAL])
def test_lock_fault_is_not_contention(tmp_path, monkeypatch, number):
    profile = _profile(tmp_path)
    fake = FakeTmux(profile.slots["main"].cwd)
    manager = ProfileTerminalManager(profile, runner=fake)
    module = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")
    operation = "locking" if os.name == "nt" else "flock"
    leases = []
    original = _Lease.acquire

    def track(lease):
        leases.append(lease)
        return original(lease)

    def fail(*args):
        raise OSError(number, "synthetic lock fault")

    monkeypatch.setattr(_Lease, "acquire", track)
    with monkeypatch.context() as patch:
        patch.setattr(module, operation, fail)
        with pytest.raises(TerminalError) as caught:
            manager.open()
    assert caught.value.code == "terminal_lock_failed"
    assert f"errno={number}" in str(caught.value)
    assert "another Codex client" not in str(caught.value)
    assert caught.value.__cause__.errno == number
    assert leases[0].handle.closed
    assert not leases[0].locked
    assert fake.calls == []
    assert not manager._leases
    assert manager.open()["created"]
    manager.close(None, action="detach")


@pytest.mark.parametrize("platform,number,busy", [
    ("nt", errno.EACCES, True), ("nt", errno.EAGAIN, True),
    ("posix", errno.EAGAIN, True), ("posix", errno.EACCES, False),
    ("nt", errno.EIO, False), ("posix", errno.ENOLCK, False),
])
def test_lock_error_classification_for_each_backend(monkeypatch, platform, number, busy):
    lease = object.__new__(_Lease)
    lease.handle = Mock()
    lease.handle.read.return_value = b"0"
    lease.locked = False
    failure = OSError(number, "synthetic lock error")
    call = Mock(side_effect=failure)
    backend = SimpleNamespace(locking=call, LK_NBLCK=1, flock=call, LOCK_EX=2, LOCK_NB=4)
    monkeypatch.setitem(sys.modules, "msvcrt" if platform == "nt" else "fcntl", backend)
    monkeypatch.setattr(terminal, "os", SimpleNamespace(name=platform))
    with pytest.raises(TerminalError) as caught:
        lease.acquire()
    assert caught.value.code == ("terminal_busy" if busy else "terminal_lock_failed")
    assert caught.value.__cause__ is failure
    lease.handle.close.assert_called_once()
    assert not lease.locked


def test_windows_lock_file_io_error_does_not_claim_an_owner(monkeypatch):
    lease = object.__new__(_Lease)
    lease.handle = Mock()
    lease.handle.seek.side_effect = OSError(errno.EACCES, "synthetic file IO failure")
    lease.locked = False
    locking = Mock()
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=1))
    monkeypatch.setattr(terminal, "os", SimpleNamespace(name="nt"))
    with pytest.raises(TerminalError) as caught:
        lease.acquire()
    assert caught.value.code == "terminal_lock_failed"
    locking.assert_not_called()
    lease.handle.close.assert_called_once()
