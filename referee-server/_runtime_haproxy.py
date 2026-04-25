"""HAProxy admin-socket mixin for ``RefereeRuntime``.

Extracted from ``scheduler.py`` as part of the god-class split tracked
in docs/AUDIT.md §8 row #2. Owns the operations that talk to the
HAProxy admin UNIX socket and that read its config file:

* ``_haproxy_listeners`` — cached parse of ``listen NAME`` blocks in
  ``HAPROXY_CONFIG_PATH``.
* ``_haproxy_server_name`` — map node host to ``n1``/``n2``/``n3`` per
  the canonical NODE_HOSTS ordering.
* ``_haproxy_socket_command`` — send a single command to the admin
  socket and return its full reply.
* ``_set_haproxy_series_state`` — flip every backend server in a
  series between ``ready`` and ``maint``.
* ``_sync_haproxy_active_series`` — given an active series number,
  put that one in ``ready`` and every other series in ``maint``.

State on ``self`` that the mixin reads / writes:

* ``self._haproxy_listener_cache`` (read + write).

It also calls ``self._series_public_ports`` from ``ComposeOpsMixin``,
so the host class must inherit from both. ``RefereeRuntime`` does.
"""
from __future__ import annotations

import logging
import re
import socket
from typing import Protocol

from config import SETTINGS
from runtime_logging import log_structured

logger = logging.getLogger("koth.referee")

LISTEN_NAME_RE = re.compile(r"^listen\s+(\S+)")


class _HasSeriesPublicPorts(Protocol):
    """Documents the cross-mixin call from this module to ComposeOpsMixin.

    The protocol stays Protocol-typed (rather than concrete) so it does
    not import the other mixin and create an artificial cycle.
    """

    def _series_public_ports(self, series: int) -> tuple[int, ...]:
        ...


class HaproxyOpsMixin:
    """Methods that talk to the HAProxy admin socket and config.

    Production callers reach these through ``RefereeRuntime``;
    the mixin needs ``self._haproxy_listener_cache`` to be initialised
    by the host class's ``__init__`` (it is, in
    ``RefereeRuntime.__init__``).
    """

    _haproxy_listener_cache: set[str] | None

    def _haproxy_listeners(self: _HasSeriesPublicPorts) -> set[str]:
        """Names of every ``listen`` block declared in the HAProxy
        config. Cached once because the config file does not change at
        runtime — operators reload HAProxy out-of-band.
        """
        # The cache lives on self; ``self`` is typed as the protocol
        # for its other-mixin call but ``RefereeRuntime`` carries the
        # cache attribute the same way.
        if getattr(self, "_haproxy_listener_cache", None) is not None:
            return self._haproxy_listener_cache  # type: ignore[return-value]
        listeners: set[str] = set()
        config_path = SETTINGS.haproxy_config_path
        if config_path.is_file():
            for raw_line in config_path.read_text(encoding="utf-8").splitlines():
                match = LISTEN_NAME_RE.match(raw_line.strip())
                if match:
                    listeners.add(match.group(1))
        self._haproxy_listener_cache = listeners  # type: ignore[attr-defined]
        return listeners

    @staticmethod
    def _haproxy_server_name(host: str) -> str | None:
        """Map ``host`` -> ``n<idx+1>`` per NODE_HOSTS order, or ``None``
        if the host is not in the configured node list. The HAProxy
        config side names servers ``n1``/``n2``/``n3`` to match.
        """
        try:
            return f"n{SETTINGS.node_hosts.index(host) + 1}"
        except ValueError:
            return None

    def _haproxy_socket_command(self, command: str) -> str:  # pragma: no cover - opens UNIX socket
        socket_path = SETTINGS.haproxy_admin_socket_path
        if not socket_path.exists():
            return ""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(socket_path))
            client.sendall((command.strip() + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = client.recv(4096)
                except TimeoutError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")

    def _set_haproxy_series_state(
        self: _HasSeriesPublicPorts,
        *,
        series: int,
        state: str,
        hosts: tuple[str, ...] | None = None,
    ) -> None:
        """Flip every backend server in ``series`` to ``state``.

        ``state`` is the HAProxy admin-socket state vocabulary
        (``ready`` / ``maint`` / ``drain``). Failures on individual
        ``set server`` commands are logged and swallowed so a single
        misconfigured backend cannot block the rest of the rotation.
        """
        socket_path = SETTINGS.haproxy_admin_socket_path
        if not socket_path.exists():
            return
        listener_names = self._haproxy_listeners()  # type: ignore[attr-defined]
        target_hosts = hosts or SETTINGS.node_hosts
        for port in self._series_public_ports(series):
            backend = f"p{port}"
            if backend not in listener_names:
                continue
            for host in target_hosts:
                server_name = self._haproxy_server_name(host)
                if not server_name:
                    continue
                try:
                    self._haproxy_socket_command(  # type: ignore[attr-defined]
                        f"set server {backend}/{server_name} state {state}"
                    )
                except Exception as exc:  # noqa: BLE001
                    log_structured(
                        logger,
                        logging.WARNING,
                        "haproxy_state_sync_failed",
                        series=series,
                        backend=backend,
                        host=host,
                        state=state,
                        error=str(exc),
                    )

    def _sync_haproxy_active_series(self, active_series: int | None) -> None:
        """Put exactly ``active_series`` in ``ready``, every other in ``maint``.

        Called whenever the runtime status transitions in or out of
        ``running`` / ``paused`` / ``rotating`` so the frontend only
        accepts traffic for the currently-active challenge set.
        """
        socket_path = SETTINGS.haproxy_admin_socket_path
        if not socket_path.exists():
            return
        for series in range(1, SETTINGS.total_series + 1):
            state = "ready" if active_series and series == active_series else "maint"
            self._set_haproxy_series_state(series=series, state=state)
