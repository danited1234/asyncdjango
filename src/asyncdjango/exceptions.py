"""Exceptions raised by asyncdjango."""

from django.core.exceptions import SynchronousOnlyOperation


class MissingNativeContext(SynchronousOnlyOperation):
    """Synchronous database I/O was attempted outside a native ORM call.

    It subclasses Django's ``SynchronousOnlyOperation`` so existing handlers for
    accidental sync ORM use in async code keep working.
    """


class NativeAsyncUnsupported(RuntimeError):
    """The operation cannot run on the native async path.

    Set ``ASYNCDJANGO["ALLOW_SYNC_FALLBACK"] = True`` to fall back to Django's
    thread-based implementation with a warning instead.
    """


class CrossTaskConnectionError(RuntimeError):
    """A task tried to use a database connection owned by another task."""
