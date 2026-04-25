"""Docker-compose remote-execution mixin for ``RefereeRuntime``.

Extracted from ``scheduler.py`` as part of the god-class split tracked
in docs/AUDIT.md §8 row #2. The mixin owns four methods:

* ``_run_compose_on_node`` — single-host compose invocation over SSH.
* ``_run_compose_parallel`` — fan-out compose invocation across every
  configured node host.
* ``_series_compose_path`` — local repo-relative path to a series'
  ``docker-compose.yml`` (used for parsing port bindings).
* ``_series_public_ports`` — cached parse of host-side ports declared
  in a series compose file.

The mixin is intentionally trivial — it pulls no concerns out of the
runtime that did not already belong here. The point of the extraction
is to put a shape on the file boundary so the next reader does not
have to scroll past 500 lines of unrelated lifecycle code to find
``_run_compose_parallel``.

State on ``self`` that the mixin reads:

* ``self.ssh_pool``  — set in ``RefereeRuntime.__init__``.
* ``self._series_port_cache`` — same.

It writes ``self._series_port_cache`` only.
"""
from __future__ import annotations

import re
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

from config import SETTINGS

# A compose port-binding line looks like ``    - "10001:80"``. The host
# port is captured by the named group; container-side port and protocol
# are intentionally ignored.
PORT_BIND_RE = re.compile(r'^\s*-\s*"(?P<host>\d+):\d+(?:/\w+)?"')


if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from ssh_client import SSHClientPool


class ComposeOpsMixin:
    """Methods for invoking ``docker compose`` on remote nodes.

    Used by ``RefereeRuntime`` via inheritance. The mixin contributes
    no ``__init__``; the host class is responsible for setting
    ``self.ssh_pool`` and ``self._series_port_cache`` before any of
    these methods are called.
    """

    # The host class must populate these. Declared here as type hints
    # to give static checkers a chance.
    ssh_pool: "SSHClientPool"
    _series_port_cache: dict[int, tuple[int, ...]]

    def _run_compose_on_node(
        self, host: str, series: int, command: str
    ) -> tuple[str, bool, str]:
        """Run a docker-compose command on one node and return
        ``(host, ok, combined_output)``. Captured exceptions become a
        ``(host, False, error_message)`` tuple so the caller can carry
        on with the other nodes rather than aborting the fan-out.
        """
        series_dir = shlex.quote(f"{SETTINGS.remote_series_root}/h{series}")
        full_command = f"cd {series_dir} && {command}"
        try:
            code, out, err = self.ssh_pool.exec(host, full_command)
            return host, code == 0, (out or err)
        except Exception as exc:  # noqa: BLE001 - we explicitly want to catch and report
            return host, False, str(exc)

    def _run_compose_parallel(
        self, series: int, command: str
    ) -> dict[str, tuple[bool, str]]:
        """Fan ``command`` out to every configured node host.

        Returns ``{host: (ok, output)}``. Empty when ``NODE_HOSTS`` is
        unset; the worker pool size is the host count so each node
        gets its own thread.
        """
        results: dict[str, tuple[bool, str]] = {}
        if not SETTINGS.node_hosts:
            return results
        with ThreadPoolExecutor(max_workers=len(SETTINGS.node_hosts)) as pool:
            futures = {
                pool.submit(self._run_compose_on_node, host, series, command): host
                for host in SETTINGS.node_hosts
            }
            for future in as_completed(futures):
                host, ok, output = future.result()
                results[host] = (ok, output)
        return results

    def _series_compose_path(self, series: int) -> Path:
        """Local repo-relative path to ``Series HN/docker-compose.yml``.

        Used by ``_series_public_ports`` to enumerate the host-side
        ports a series exposes; it is a parse, not a deploy artifact.
        Production deploys read the per-node ``hN/docker-compose.yml``
        instead, which is staged separately by the deployment runbook.
        """
        return Path(__file__).resolve().parents[1] / f"Series H{series}" / "docker-compose.yml"

    def _series_public_ports(self, series: int) -> tuple[int, ...]:
        """Memoised list of host ports declared in a series compose file.

        Parsed once per ``RefereeRuntime`` instance and cached on
        ``self._series_port_cache`` to avoid re-reading the file on
        every poll.
        """
        cached = self._series_port_cache.get(series)
        if cached is not None:
            return cached
        compose_path = self._series_compose_path(series)
        ports: list[int] = []
        if compose_path.is_file():
            for raw_line in compose_path.read_text(encoding="utf-8").splitlines():
                match = PORT_BIND_RE.match(raw_line)
                if match:
                    ports.append(int(match.group("host")))
        resolved = tuple(sorted(set(ports)))
        self._series_port_cache[series] = resolved
        return resolved
