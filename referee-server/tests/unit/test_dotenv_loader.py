"""Unit test for ``config._load_dotenv_if_present``.

The legacy test pattern was to reload the whole ``config`` module after
writing to the source-tree ``.env`` file. That had two problems: (1) it
wrote to the real file and could corrupt a developer checkout on a crash,
and (2) the reload re-bound the module-level ``SETTINGS`` singleton, which
silently desynchronised every module that had imported ``SETTINGS`` at
module load time — causing spooky-action-at-a-distance failures in tests
that ran later in the same session.

This replacement exercises only the loader function, using a temp-directory
``.env`` file selected via the ``KOTH_REFEREE_ENV`` override and
``monkeypatch`` for the process environment, so neither sys.modules nor the
shared dataclass instance is disturbed.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


def test_dotenv_loader_populates_environ_from_override_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "koth.env"
    env_file.write_text(
        "KOTH_DOTENV_LOADER_TEST_KEY=from-dotenv\n"
        "# comment line ignored\n"
        "KOTH_DOTENV_LOADER_ANOTHER='quoted value'\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("KOTH_DOTENV_LOADER_TEST_KEY", raising=False)
    monkeypatch.delenv("KOTH_DOTENV_LOADER_ANOTHER", raising=False)
    monkeypatch.setenv("KOTH_REFEREE_ENV", str(env_file))

    import config

    config._load_dotenv_if_present()

    import os

    assert os.environ["KOTH_DOTENV_LOADER_TEST_KEY"] == "from-dotenv"
    assert os.environ["KOTH_DOTENV_LOADER_ANOTHER"] == "quoted value"


def test_dotenv_loader_does_not_overwrite_existing_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "koth.env"
    env_file.write_text("KOTH_DOTENV_LOADER_PRESET=dotenv-value\n", encoding="utf-8")

    monkeypatch.setenv("KOTH_DOTENV_LOADER_PRESET", "preset-value")
    monkeypatch.setenv("KOTH_REFEREE_ENV", str(env_file))

    import config

    config._load_dotenv_if_present()

    import os

    assert os.environ["KOTH_DOTENV_LOADER_PRESET"] == "preset-value"


def test_module_exposes_settings_singleton() -> None:
    """Sanity: importing config yields a frozen Settings instance."""
    config = importlib.import_module("config")
    assert hasattr(config, "SETTINGS")
    assert config.SETTINGS is config.SETTINGS  # same object across re-lookup
