"""Shared fixtures and test doubles for the referee-server test suite.

Everything in this module is test infrastructure — no production code depends
on it. Three goals drove the design:

* **Determinism.** All mutations of the frozen ``SETTINGS`` singleton happen
  through the ``settings_override`` fixture so every test gets a clean restore
  on teardown, even on error. No test should touch ``SETTINGS`` directly.

* **Speed.** Fixtures that instantiate a ``Database`` or a ``RefereeRuntime``
  are function-scoped so tests stay independent, but the ``app_instance``
  fixture is module-scoped so FastAPI route collection happens once per file.

* **Clarity.** Synthetic snapshots — the nine-element "all variants on all
  nodes healthy" matrix — were copy-pasted 17+ times in the old layout. The
  ``healthy_matrix`` and ``snapshot`` helpers replace the copy-paste.
"""
from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from fastapi.responses import HTMLResponse

from config import SETTINGS
from db import Database
from poller import VariantSnapshot
from scheduler import RefereeRuntime


# ---------------------------------------------------------------------------
# Default values used across the suite. Chosen to match the shape asserted in
# the legacy tests (3 nodes, variants A/B/C, quorum of 2) so behavior is
# preserved while the test scaffolding changes.
# ---------------------------------------------------------------------------
DEFAULT_NODE_HOSTS: tuple[str, ...] = ("192.168.0.102", "192.168.0.103", "192.168.0.106")
DEFAULT_NODE_PRIORITY: tuple[str, ...] = DEFAULT_NODE_HOSTS
DEFAULT_VARIANTS: tuple[str, ...] = ("A", "B", "C")
DEFAULT_MIN_HEALTHY_NODES: int = 2


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class DummySSH:
    """Recording ``SSHClientPool`` stand-in.

    Every ``exec()`` call is appended to ``commands`` as ``(host, command)``.
    Returns a successful exit code by default so callers that do not care
    about the response can ignore it; tests that do care can subclass or set
    ``response`` to a custom tuple.
    """

    def __init__(self, response: tuple[int, str, str] = (0, "OK", "")) -> None:
        self.commands: list[tuple[str, str]] = []
        self.response = response

    def exec(self, host: str, command: str) -> tuple[int, str, str]:
        self.commands.append((host, command))
        return self.response

    def close(self) -> None:  # pragma: no cover - nothing to clean up
        return


class DummyScheduler:
    """Minimal APScheduler stand-in that records jobs in a plain dict.

    The production ``BackgroundScheduler`` spawns a thread, consults its
    own persistence layer, and fires jobs on wall-clock intervals. None of
    that belongs in a unit test. This fake accepts the same ``add_job`` /
    ``remove_job`` / ``get_job`` surface and never runs any code.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}
        self.started = False

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = False) -> None:
        _ = wait
        self.jobs.clear()

    def get_job(self, job_id: str) -> dict[str, object] | None:
        return self.jobs.get(job_id)

    def get_jobs(self) -> list[types.SimpleNamespace]:
        return [types.SimpleNamespace(id=job_id) for job_id in sorted(self.jobs)]

    def add_job(
        self,
        func: object,
        trigger: object,
        *,
        id: str,
        replace_existing: bool = False,
        max_instances: int = 1,
        **kwargs: object,
    ) -> None:
        _ = func, replace_existing, max_instances
        self.jobs[id] = {"trigger": trigger, **kwargs}

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


class DummyTemplates:
    """Jinja2Templates stand-in used by ``ApiEndpointTests``.

    Every template render returns a static HTML body so that template-rendering
    tests only verify routing and content-type, not the template content. Full
    template snapshots belong in a separate layer when we add one.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs

    def TemplateResponse(self, *args: object, **kwargs: object) -> HTMLResponse:
        _ = args, kwargs
        return HTMLResponse("<html><body>ok</body></html>")


# ---------------------------------------------------------------------------
# settings_override — the only sanctioned way to mutate SETTINGS in a test
# ---------------------------------------------------------------------------
@contextmanager
def _apply_settings_overrides(**overrides: Any) -> Iterator[None]:
    """Swap fields on the frozen ``SETTINGS`` dataclass with guaranteed restore.

    ``Settings`` is ``@dataclass(frozen=True)`` so we use ``object.__setattr__``
    to bypass the immutability guard. This is a test-only escape hatch; no
    production code should ever do this. The contextmanager guarantees that
    every overridden field is restored to its original value, even if the body
    raises.
    """
    originals = {name: getattr(SETTINGS, name) for name in overrides}
    for name, value in overrides.items():
        object.__setattr__(SETTINGS, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            object.__setattr__(SETTINGS, name, value)


@pytest.fixture
def settings_override() -> Iterator[Any]:
    """Yield a factory returning a contextmanager that applies overrides.

    Example::

        def test_thing(settings_override):
            with settings_override(admin_api_key="test-key", min_healthy_nodes=1):
                ...

    Overrides apply on ``__enter__`` and are reverted on ``__exit__`` — even
    on exception — so no test can leak ``SETTINGS`` mutations into the next.
    The fixture is stateless; the contextmanager carries its own snapshot.
    """
    yield _apply_settings_overrides


@pytest.fixture(autouse=True)
def _default_runtime_settings() -> Iterator[None]:
    """Stabilise the shared ``SETTINGS`` singleton for every test in the suite.

    Legacy tests called ``_override_runtime_settings`` in every ``setUp`` to
    pin node hosts, priority, variants, and the quorum threshold. Doing it
    autouse here removes ~40 repetitions and makes the defaults explicit.
    """
    with _apply_settings_overrides(
        node_hosts=DEFAULT_NODE_HOSTS,
        node_priority=DEFAULT_NODE_PRIORITY,
        variants=DEFAULT_VARIANTS,
        min_healthy_nodes=DEFAULT_MIN_HEALTHY_NODES,
    ):
        yield


# ---------------------------------------------------------------------------
# Database & runtime fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_db(tmp_path: Path) -> Iterator[Database]:
    """Yield an initialised ``Database`` backed by a fresh SQLite file.

    ``tmp_path`` is the pytest-managed per-test directory, so cleanup is
    automatic. Using ``tmp_path`` instead of ``tempfile.mkstemp`` means the
    database file lives next to the test's other artefacts on failure, which
    makes post-mortem inspection easier.
    """
    db_path = tmp_path / "referee.db"
    db = Database(db_path)
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def dummy_ssh() -> DummySSH:
    """Fresh recording SSH double per test."""
    return DummySSH()


@pytest.fixture
def dummy_scheduler() -> DummyScheduler:
    """Fresh in-memory scheduler double per test."""
    return DummyScheduler()


@pytest.fixture
def runtime(tmp_db: Database, dummy_ssh: DummySSH) -> RefereeRuntime:
    """A ``RefereeRuntime`` wired to the tmp DB and a recording SSH.

    The runtime's real ``BackgroundScheduler`` is *not* started here; tests
    that need job scheduling should replace ``runtime.scheduler`` with the
    ``dummy_scheduler`` fixture and call ``runtime.start_scheduler()``.
    """
    return RefereeRuntime(tmp_db, dummy_ssh)


# ---------------------------------------------------------------------------
# Snapshot helpers — replaces the 17+ copy-pastes of "9 healthy snapshots"
# ---------------------------------------------------------------------------
def snapshot(
    *,
    node_host: str,
    variant: str = "A",
    king: str | None = "Team Alpha",
    king_mtime_epoch: int | None = 1000,
    status: str = "running",
    node_epoch: int | None = 1000,
    extra_sections: dict[str, str] | None = None,
) -> VariantSnapshot:
    """Build a ``VariantSnapshot`` with sensible defaults.

    Tests that only care about one or two fields can override them and let the
    rest match the "healthy, team-owned variant" baseline. Tests that care
    about the probe sections (``AUTHKEYS``, ``SHADOW``, ``CRON``, etc.) can
    pass ``extra_sections``.
    """
    sections: dict[str, str] = {}
    if node_epoch is not None:
        sections["NODE_EPOCH"] = str(node_epoch)
    if extra_sections:
        sections.update(extra_sections)
    return VariantSnapshot(
        node_host=node_host,
        variant=variant,
        king=king,
        king_mtime_epoch=king_mtime_epoch,
        status=status,
        sections=sections,
        checked_at=datetime.now(UTC),
    )


def healthy_matrix(
    *,
    hosts: Iterable[str] = DEFAULT_NODE_HOSTS,
    variants: Iterable[str] = DEFAULT_VARIANTS,
    king: str | None = "unclaimed",
    king_mtime_epoch: int = 1000,
    node_epoch: int = 1000,
    extra_sections: dict[str, str] | None = None,
) -> list[VariantSnapshot]:
    """Return the full (hosts × variants) grid of healthy snapshots.

    ``king="unclaimed"`` matches the most common scenario in the legacy tests:
    a post-deploy state with no owner. Override ``king`` for the rare case
    where a team already owns a variant.
    """
    return [
        snapshot(
            node_host=host,
            variant=variant,
            king=king,
            king_mtime_epoch=king_mtime_epoch,
            node_epoch=node_epoch,
            extra_sections=extra_sections,
        )
        for host in hosts
        for variant in variants
    ]


# ---------------------------------------------------------------------------
# FastAPI app fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def app_instance(
    settings_override: Any,
    tmp_db: Database,
    runtime: RefereeRuntime,
    dummy_scheduler: DummyScheduler,
) -> Iterator[types.ModuleType]:
    """Return the ``app`` module with its globals rebound to the test doubles.

    This replaces the 40-line ``ApiEndpointTests.setUp`` dance:

    1. Pin an admin API key so ``require_admin_api_key`` does not reject tests.
    2. Use the tmp DB, the DummySSH-backed runtime, and a DummyScheduler.
    3. Patch ``fastapi.templating.Jinja2Templates`` with ``DummyTemplates`` so
       importing ``app`` does not try to load real Jinja templates.
    4. Import (or re-import) the ``app`` module with that patch in place.
    5. Rebind the module-level ``db``, ``runtime``, and ``ssh_pool`` globals
       to our instances; restore them on teardown.

    The patch of ``Jinja2Templates`` is kept in scope until the app module has
    finished importing — after that, the module has captured its reference
    and no further patching is needed.
    """
    with settings_override(admin_api_key="test-admin-key"):
        runtime.scheduler = dummy_scheduler
        runtime.start_scheduler = Mock()  # type: ignore[assignment]
        runtime.shutdown = Mock()  # type: ignore[assignment]

        sys.modules.pop("app", None)
        with patch("fastapi.templating.Jinja2Templates", DummyTemplates):
            app_module = importlib.import_module("app")

        original_db = app_module.db
        original_runtime = app_module.runtime
        original_ssh_pool = app_module.ssh_pool
        app_module.db = tmp_db
        app_module.runtime = runtime
        app_module.ssh_pool = runtime.ssh_pool
        try:
            yield app_module
        finally:
            app_module.db = original_db
            app_module.runtime = original_runtime
            app_module.ssh_pool = original_ssh_pool


# ---------------------------------------------------------------------------
# Misc: a helper for tests that need to swap the source-tree .env without
# risking the developer's real file.
# ---------------------------------------------------------------------------
@contextmanager
def temporary_dotenv(content: str, target_dir: Path) -> Iterator[Path]:
    """Write ``content`` to ``target_dir/.env`` and restore on exit.

    Used by the config-loading test. Unlike the legacy version, this restores
    an absent file to absent rather than writing an empty file back.
    """
    env_path = target_dir / ".env"
    original_text: str | None = None
    if env_path.exists():
        original_text = env_path.read_text(encoding="utf-8")
    env_path.write_text(content, encoding="utf-8")
    try:
        yield env_path
    finally:
        if original_text is None:
            if env_path.exists():
                env_path.unlink()
        else:
            env_path.write_text(original_text, encoding="utf-8")


# Ensure a sane import path even when pytest is invoked from unusual cwds.
# (The top-of-tree conftest at ``referee-server/conftest.py`` is the primary
# point of sys.path setup; this is belt-and-braces for CI runners.)
_REFEREE_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_REFEREE_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_REFEREE_SERVER_DIR))

# Silence unused-import warnings from linters for symbols re-exported as
# fixture returns / test helpers.
__all__ = [
    "DEFAULT_MIN_HEALTHY_NODES",
    "DEFAULT_NODE_HOSTS",
    "DEFAULT_NODE_PRIORITY",
    "DEFAULT_VARIANTS",
    "DummySSH",
    "DummyScheduler",
    "DummyTemplates",
    "healthy_matrix",
    "snapshot",
    "temporary_dotenv",
]
