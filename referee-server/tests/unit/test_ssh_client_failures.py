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


# ---------------------------------------------------------------------------
# Edge cases for _split_target.
#
# The target-string parser sits between the NODE_SSH_TARGETS env var and
# paramiko.connect. A malformed string should fall back to the default
# username + the raw target string, not crash or silently use an empty
# username. These tests lock that contract in.
# ---------------------------------------------------------------------------
def test_split_target_without_at_uses_default_username() -> None:
    pool = SSHClientPool(
        username="ops",
        private_key_path="~/.ssh/id_rsa",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=False,
    )
    assert pool._split_target("192.168.0.102") == ("ops", "192.168.0.102")


def test_split_target_with_user_and_host() -> None:
    pool = SSHClientPool(
        username="ops",
        private_key_path="~/.ssh/id_rsa",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=False,
    )
    assert pool._split_target("node-a@192.168.0.102") == ("node-a", "192.168.0.102")


def test_split_target_with_only_at_symbol_falls_back() -> None:
    # "@host" has an empty username → fall back to default + the raw
    # target, which will then fail at paramiko.connect time with a clear
    # hostname error rather than silently connecting as an empty user.
    pool = SSHClientPool(
        username="ops",
        private_key_path="~/.ssh/id_rsa",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=False,
    )
    assert pool._split_target("@192.168.0.102") == ("ops", "@192.168.0.102")


def test_split_target_with_trailing_at_falls_back() -> None:
    pool = SSHClientPool(
        username="ops",
        private_key_path="~/.ssh/id_rsa",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=False,
    )
    assert pool._split_target("root@") == ("ops", "root@")


def test_split_target_lone_at_falls_back() -> None:
    pool = SSHClientPool(
        username="ops",
        private_key_path="~/.ssh/id_rsa",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=False,
    )
    assert pool._split_target("@") == ("ops", "@")


def test_split_target_rsplit_takes_last_at_as_separator() -> None:
    # ``user@email@host`` is malformed but rsplit on the right-most @
    # produces a plausible username ("user@email") and hostname ("host").
    # The point of this test is to document that the parser uses rsplit,
    # not split — if a future refactor switches to ``split("@", 1)`` the
    # behavior changes and this test fails.
    pool = SSHClientPool(
        username="ops",
        private_key_path="~/.ssh/id_rsa",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=False,
    )
    assert pool._split_target("user@email@host") == ("user@email", "host")


# ---------------------------------------------------------------------------
# Edge cases for _resolve_target / host override behavior.
# ---------------------------------------------------------------------------
def test_resolve_target_returns_host_when_no_override() -> None:
    pool = _make_pool()
    assert pool._resolve_target("192.168.0.102") == "192.168.0.102"


def test_resolve_target_returns_override_when_present() -> None:
    pool = SSHClientPool(
        username="ops",
        private_key_path="~/.ssh/id_rsa",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=False,
        host_target_overrides={"192.168.0.102": "node-a@10.0.0.1"},
    )
    assert pool._resolve_target("192.168.0.102") == "node-a@10.0.0.1"


def test_resolve_target_ignores_unrelated_overrides() -> None:
    # Only the host we asked for is substituted; siblings are untouched.
    pool = SSHClientPool(
        username="ops",
        private_key_path="~/.ssh/id_rsa",
        port=22,
        timeout_seconds=2,
        strict_host_key_checking=False,
        host_target_overrides={"192.168.0.103": "node-b@10.0.0.2"},
    )
    assert pool._resolve_target("192.168.0.102") == "192.168.0.102"


# ---------------------------------------------------------------------------
# Close / lifecycle edge cases.
# ---------------------------------------------------------------------------
def test_close_on_empty_pool_is_noop() -> None:
    pool = _make_pool()
    # No exec has happened yet — closing must not raise.
    pool.close()


def test_close_is_idempotent_on_double_call() -> None:
    pool = _make_pool()
    pool.close()
    pool.close()  # second call must also be a no-op


def test_strict_host_key_policy_is_reject_when_strict_true() -> None:
    # Verify the constructor wiring: strict=True must install
    # paramiko.RejectPolicy (fails on unknown host keys). strict=False
    # installs AutoAddPolicy (trusts first contact).
    recorded: list[object] = []

    class _PolicyRecorder(_CountingFakeClient):
        def set_missing_host_key_policy(self, policy) -> None:
            recorded.append(type(policy))

    with patch("ssh_client.paramiko.SSHClient", return_value=_PolicyRecorder(connect_behavior="auth")):
        strict_pool = SSHClientPool(
            username="ops",
            private_key_path="~/.ssh/id_rsa",
            port=22,
            timeout_seconds=2,
            strict_host_key_checking=True,
        )
        with pytest.raises(paramiko.AuthenticationException):
            strict_pool.exec("192.168.0.102", "whoami")

    assert recorded == [paramiko.RejectPolicy]


def test_strict_host_key_policy_is_autoadd_when_strict_false() -> None:
    recorded: list[object] = []

    class _PolicyRecorder(_CountingFakeClient):
        def set_missing_host_key_policy(self, policy) -> None:
            recorded.append(type(policy))

    with patch("ssh_client.paramiko.SSHClient", return_value=_PolicyRecorder(connect_behavior="auth")):
        lax_pool = SSHClientPool(
            username="ops",
            private_key_path="~/.ssh/id_rsa",
            port=22,
            timeout_seconds=2,
            strict_host_key_checking=False,
        )
        with pytest.raises(paramiko.AuthenticationException):
            lax_pool.exec("192.168.0.102", "whoami")

    assert recorded == [paramiko.AutoAddPolicy]


def test_private_key_path_is_tilde_expanded() -> None:
    # Paramiko is called with the tilde-expanded absolute path, not the
    # raw ``~/.ssh/...`` string. Without expansion, OpenSSH on some
    # platforms fails to read the key.
    recorded: list[dict[str, object]] = []

    class _RecordingClient(_CountingFakeClient):
        def connect(self, **kwargs) -> None:
            recorded.append(kwargs)
            raise paramiko.AuthenticationException("stop here")

    with patch("ssh_client.paramiko.SSHClient", return_value=_RecordingClient(connect_behavior="auth")):
        pool = SSHClientPool(
            username="ops",
            private_key_path="~/.ssh/koth_referee",
            port=22,
            timeout_seconds=2,
            strict_host_key_checking=False,
        )
        with pytest.raises(paramiko.AuthenticationException):
            pool.exec("192.168.0.102", "hostname")

    assert recorded, "paramiko.connect was never invoked"
    key_path = str(recorded[0]["key_filename"])
    assert "~" not in key_path, f"tilde was not expanded in {key_path!r}"
