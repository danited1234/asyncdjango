"""Opt-in native async ORM for projects that set ``PATCH_ORM = False``.

With patching enabled (the default) every Django model is native and these
classes are unnecessary. Without it, opt models in individually::

    from asyncdjango.db.models import NativeManager, NativeModelMixin

    class Order(NativeModelMixin, models.Model):
        objects = NativeManager()

``NativeManager``/``NativeQuerySet`` make the queryset async methods native,
and ``NativeModelMixin`` does the same for ``asave()``, ``adelete()`` and
``arefresh_from_db()``. Related managers (``aadd()``, ``aset()``, ...) keep
Django's thread-based implementation unless ``PATCH_ORM`` is enabled.
"""

from __future__ import annotations

from django.db import models

from .patch import native_model_methods, native_queryset_methods

NativeQuerySet = type("NativeQuerySet", (models.QuerySet,), {
    "__module__": __name__,
    "__doc__": "A QuerySet whose async methods run natively.",
    **native_queryset_methods(),
})


class NativeManager(models.Manager.from_queryset(NativeQuerySet)):
    """A Manager whose async methods run natively."""


NativeModelMixin = type("NativeModelMixin", (), {
    "__module__": __name__,
    "__doc__": "Native asave(), adelete() and arefresh_from_db() for a model.",
    **native_model_methods(),
})
