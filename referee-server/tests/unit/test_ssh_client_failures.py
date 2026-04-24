"""Direct unit tests for ``SSHClientPool`` failure paths.

The happy path is covered in ``test_ssh_client.py``. These tests pin the
behavior under failure conditions: authentication errors, connection
timeouts, and that a failed ``exec`` resets the cached client so the next
call reconnects rather than reusing a broken socket.
"""
from __future__ import annotations

import socket
from unittest.mock import patch

import paramiko
import pytest

from ssh_client import SSHClientPool


pytestmark = pytest.mark.unit


class _CountingFakeClient:
    """Paramiko stand-in that records how many times ``connect`` was called.

    Each instance can be scripted with ``connect_behavior`` to succeed or to
    raise on ``connect``. Closing is counted so tests can assert that a
    failed exec triggered reset_host().
    """

    def __init__(self, *, connect_behavior: str = "ok") -> None:
        self.connect_behavior = connect_behavior
        self.connect_calls = 0
        self.close_calls = 0

    def load_system_host_keys(self) -> None:
        return

    def set_missing_host_key_policy(self, policy) -> None:
        _ = policy

    def connect(self, **_: object) -> None:
        self.connect_calls += 1
        if self.connect_behavior == "auth":
            raise paramiko.AuthenticationException("bad key")
        if self.connect_behavior == "timeout":
            raise socket.timeout("connect timed out")
        if self.connect_behavior == "sshexc":
            raise paramiko.SSHException("handshake failed")

    def exec_command(self, command: str, timeout: int):  # pragma: no cover - not reached in failure tests
        raise AssertionError("exec_command should not be reached after a failed connect")

    def close(self) -> None:
        self.close_calls += 1


def _make_pool(strict: bool = True) -> SSHClientPool:
    return SSHClientPool(
        username="root",
        private_key_path="~/.ssh/koth_referee",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=strict,
    )


def test_exec_propagates_authentication_error_and_resets_client() -> None:
    fake = _CountingFakeClient(connect_behavior="auth")
    with patch("ssh_client.paramiko.SSHClient", return_value=fake):
        pool = _make_pool()
        with pytest.raises(paramiko.AuthenticationException):
            pool.exec("192.168.0.102", "whoami")

    # The pool caches clients; a failing connect must not leave a half-
    # initialised entry in the cache.
    assert "192.168.0.102" not in pool._clients
    assert "nodeA@192.168.0.102" not in pool._clients


def test_exec_propagates_timeout_and_resets_client() -> None:
    fake = _CountingFakeClient(connect_behavior="timeout")
    with patch("ssh_client.paramiko.SSHClient", return_value=fake):
        pool = _make_pool(strict=False)
        with pytest.raises(socket.timeout):
            pool.exec("192.168.0.103", "hostname")

    assert "192.168.0.103" not in pool._clients


def test_exec_propagates_ssh_exception_and_resets_client() -> None:
    fake = _CountingFakeClient(connect_behavior="sshexc")
    with patch("ssh_client.paramiko.SSHClient", return_value=fake):
        pool = _make_pool()
        with pytest.raises(paramiko.SSHException):
            pool.exec("192.168.0.104", "uptime")

    assert "192.168.0.104" not in pool._clients


def test_second_exec_attempt_rebuilds_a_fresh_client_after_failure() -> None:
    fakes: list[_CountingFakeClient] = []

    def _factory(*_: object, **__: object) -> _CountingFakeClient:
        instance = _CountingFakeClient(connect_behavior="auth")
        fakes.append(instance)
        return instance

    with patch("ssh_client.paramiko.SSHClient", side_effect=_factory):
        pool = _make_pool()
        with pytest.raises(paramiko.AuthenticationException):
            pool.exec("192.168.0.105", "one")
        with pytest.raises(paramiko.AuthenticationException):
            pool.exec("192.168.0.105", "two")

    # Two independent client instances were created — no stale cache reuse.
    assert len(fakes) == 2
    assert fakes[0] is not fakes[1]


def test_close_closes_all_cached_clients_and_clears_cache() -> None:
    fake_ok = _CountingFakeClient(connect_behavior="ok")

    class _OkStream:
        def read(self) -> bytes:
            return b""
        channel = type("_Channel", (), {"recv_exit_status": staticmethod(lambda: 0)})()

    fake_ok.exec_command = lambda command, timeout: (None, _OkStream(), _OkStream())

    with patch("ssh_client.paramiko.SSHClient", return_value=fake_ok):
        pool = _make_pool()
        pool.exec("192.168.0.110", "echo")
        assert pool._clients

        pool.close()

    assert pool._clients == {}
    assert fake_ok.close_calls == 1


def test_reset_host_is_a_noop_for_unknown_host() -> None:
    pool = _make_pool()
    # Should not raise.
    pool.reset_host("192.168.99.99")
