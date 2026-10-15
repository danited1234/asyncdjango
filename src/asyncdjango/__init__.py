"""Native async Django: the ORM's async API without worker threads.

Add ``"asyncdjango"`` to ``INSTALLED_APPS``. See README.md for settings.
"""

from typing import Any

__version__ = "0.1.0.dev0"

__all__ = ["run_native", "sync_to_native"]


def __getattr__(name: str) -> Any:
    # Importing the database layer requires configured Django settings, so
    # load it lazily: `import asyncdjango` must work before django.setup().
    if name in __all__:
        from . import db

        return getattr(db, name)
    raise AttributeError(f"module 'asyncdjango' has no attribute {name!r}")
