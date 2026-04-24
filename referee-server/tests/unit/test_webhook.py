"""Direct unit tests for ``webhook.send_webhook`` and ``fire_and_forget``.

``send_webhook`` is a thin wrapper around httpx.AsyncClient.post. The
behavior we care about is:

* when WEBHOOK_URL is empty, the function is a no-op and does not open an
  HTTP client (important — httpx raises if you pass an empty URL);
* when httpx raises, the exception is swallowed and reported through the
  ``koth.referee`` logger at ERROR, not propagated to the scheduler thread;
* ``fire_and_forget`` schedules the coroutine on an existing loop when one
  is running, and spawns a daemon thread when one is not — in both cases
  the coroutine must actually be awaited, never leaked.

The ``koth.referee`` logger is configured with ``propagate = False`` by
``runtime_logging.configure_logging``, so pytest's ``caplog`` fixture
cannot see its records via the root-logger path. These tests patch the
module-level logger instead of relying on caplog, which is both more
explicit and isolated from whatever logging config has been applied
earlier in the session.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import webhook


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_send_webhook_is_noop_when_url_unset(settings_override: Any) -> None:
    with settings_override(webhook_url=""):
        with patch("webhook.httpx.AsyncClient") as client_ctor:
            await webhook.send_webhook({"event": "test"})
            client_ctor.assert_not_called()


@pytest.mark.asyncio
async def test_send_webhook_posts_payload_to_configured_url(settings_override: Any) -> None:
    with settings_override(webhook_url="https://example.test/hook"):
        with patch("webhook.httpx.AsyncClient") as client_ctor:
            client = MagicMock()
            client.post = AsyncMock()
            client_ctor.return_value.__aenter__.return_value = client
            client_ctor.return_value.__aexit__.return_value = None

            await webhook.send_webhook({"event": "rotate", "series": 2})

            client.post.assert_awaited_once()
            (url,), kwargs = client.post.call_args
            assert url == "https://example.test/hook"
            assert kwargs["json"] == {"event": "rotate", "series": 2}


@pytest.mark.asyncio
async def test_send_webhook_swallows_and_logs_post_errors(settings_override: Any) -> None:
    with settings_override(webhook_url="https://example.test/hook"):
        with patch("webhook.httpx.AsyncClient") as client_ctor, patch(
            "webhook.logger"
        ) as logger_mock:
            client = MagicMock()
            client.post = AsyncMock(side_effect=RuntimeError("boom"))
            client_ctor.return_value.__aenter__.return_value = client
            client_ctor.return_value.__aexit__.return_value = None

            await webhook.send_webhook({"event": "x"})

            assert logger_mock.error.called
            (template, exc), _kwargs = logger_mock.error.call_args
            assert "webhook delivery failed" in template
            assert isinstance(exc, RuntimeError)


def test_fire_and_forget_spawns_thread_when_no_loop_running() -> None:
    # fire_and_forget's thread-spawn path does not evaluate send_webhook
    # eagerly — the coroutine is created lazily inside the thread's target
    # lambda, which never runs when Thread is patched. No coroutine leaks.
    with patch("webhook.threading.Thread") as thread_ctor:
        thread = MagicMock()
        thread_ctor.return_value = thread

        webhook.fire_and_forget({"event": "no-loop"})

        thread_ctor.assert_called_once()
        thread.start.assert_called_once()


@pytest.mark.asyncio
async def test_fire_and_forget_uses_running_loop_when_available() -> None:
    loop = asyncio.get_running_loop()

    # ``send_webhook`` is an async def, so unittest.mock.patch replaces it
    # with an AsyncMock whose call returns a real coroutine. We want to
    # avoid leaking that coroutine, so we force a plain MagicMock whose
    # call returns a sentinel object — never awaited, never garbage-collected
    # as a coroutine.
    send_stub = MagicMock(name="send_webhook_stub")
    sentinel = object()
    send_stub.return_value = sentinel

    with patch.object(loop, "create_task") as create_task, patch(
        "webhook.send_webhook", new=send_stub
    ):
        webhook.fire_and_forget({"event": "with-loop"})

    create_task.assert_called_once_with(sentinel)


# ---------------------------------------------------------------------------
# Edge cases for send_webhook error paths.
#
# The production contract is: send_webhook NEVER raises. Every failure
# mode — timeout, connection refused, HTTP error status, broken JSON —
# must be swallowed and logged so a flaky webhook receiver does not take
# the scheduler thread with it.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_send_webhook_swallows_timeout_error(settings_override: Any) -> None:
    import httpx as _httpx

    with settings_override(webhook_url="https://example.test/hook"):
        with patch("webhook.httpx.AsyncClient") as client_ctor, patch(
            "webhook.logger"
        ) as logger_mock:
            client = MagicMock()
            client.post = AsyncMock(side_effect=_httpx.TimeoutException("slow receiver"))
            client_ctor.return_value.__aenter__.return_value = client
            client_ctor.return_value.__aexit__.return_value = None

            # The contract is "never raise"; any exception here fails the test.
            await webhook.send_webhook({"event": "x"})

            assert logger_mock.error.called


@pytest.mark.asyncio
async def test_send_webhook_swallows_os_error(settings_override: Any) -> None:
    with settings_override(webhook_url="https://example.test/hook"):
        with patch("webhook.httpx.AsyncClient") as client_ctor, patch(
            "webhook.logger"
        ) as logger_mock:
            client = MagicMock()
            client.post = AsyncMock(side_effect=OSError("connection refused"))
            client_ctor.return_value.__aenter__.return_value = client
            client_ctor.return_value.__aexit__.return_value = None

            await webhook.send_webhook({"event": "x"})

            assert logger_mock.error.called


@pytest.mark.asyncio
async def test_send_webhook_does_not_raise_on_non_serializable_payload(
    settings_override: Any,
) -> None:
    # httpx.AsyncClient.post with ``json=<non-serializable>`` would raise
    # TypeError from the JSON encoder. send_webhook's blanket ``except
    # Exception`` must catch this; the scheduler thread cannot afford to
    # die because a caller tossed a datetime into the payload.
    class _NotSerializable:
        pass

    with settings_override(webhook_url="https://example.test/hook"):
        with patch("webhook.httpx.AsyncClient") as client_ctor, patch(
            "webhook.logger"
        ) as logger_mock:
            client = MagicMock()
            client.post = AsyncMock(side_effect=TypeError("not json-encodable"))
            client_ctor.return_value.__aenter__.return_value = client
            client_ctor.return_value.__aexit__.return_value = None

            await webhook.send_webhook({"event": "x", "payload": _NotSerializable()})

            assert logger_mock.error.called


@pytest.mark.asyncio
async def test_send_webhook_accepts_5xx_response_without_raising(
    settings_override: Any,
) -> None:
    # httpx does NOT raise on non-2xx by default — the caller has to
    # call raise_for_status() for that. Our send_webhook deliberately
    # does NOT call raise_for_status() because a webhook receiver that
    # returns 500 should still count as "delivered"; the receiver is
    # the one that needs to be fixed, not the referee. This test
    # pins that contract.
    response = MagicMock()
    response.status_code = 503
    with settings_override(webhook_url="https://example.test/hook"):
        with patch("webhook.httpx.AsyncClient") as client_ctor, patch(
            "webhook.logger"
        ) as logger_mock:
            client = MagicMock()
            client.post = AsyncMock(return_value=response)
            client_ctor.return_value.__aenter__.return_value = client
            client_ctor.return_value.__aexit__.return_value = None

            await webhook.send_webhook({"event": "x"})

            # post ran, no error was logged, no exception propagated.
            client.post.assert_awaited_once()
            assert not logger_mock.error.called


@pytest.mark.asyncio
async def test_send_webhook_skips_when_url_is_whitespace_only(settings_override: Any) -> None:
    # The production check is ``if not SETTINGS.webhook_url``; an env value
    # of ``" "`` is truthy in Python and would cause an httpx call to a
    # whitespace URL. This test pins the present behavior: a whitespace
    # URL IS sent (not skipped). If a future fix treats whitespace as
    # empty, this test flips and surfaces the change.
    with settings_override(webhook_url="   "):
        with patch("webhook.httpx.AsyncClient") as client_ctor:
            client = MagicMock()
            client.post = AsyncMock()
            client_ctor.return_value.__aenter__.return_value = client
            client_ctor.return_value.__aexit__.return_value = None

            await webhook.send_webhook({"event": "x"})

            # Current behavior: AsyncClient IS opened because "   " is truthy.
            client_ctor.assert_called_once()
