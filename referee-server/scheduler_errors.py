"""Shared error type for the runtime mixins.

Lifted out of ``scheduler.py`` so the mixin modules can import
``RuntimeGuardError`` without creating an import cycle:
``scheduler.py`` imports the mixin modules at the top, the mixin
modules raise ``RuntimeGuardError`` from inside their methods, and
``scheduler.py`` re-exports the symbol for callers that historically
imported it from there (``from scheduler import RuntimeGuardError``).
"""
from __future__ import annotations


class RuntimeGuardError(RuntimeError):
    """Raised when a lifecycle method refuses to act because the FSM
    is in a state that does not permit the requested transition. Used
    by the runtime mixins and by ``RefereeRuntime`` itself; surfaced
    to API callers as HTTP 409 by ``app.run_admin_action``.
    """
