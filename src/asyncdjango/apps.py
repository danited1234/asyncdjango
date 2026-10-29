from django.apps import AppConfig


class AsyncDjangoConfig(AppConfig):
    name = "asyncdjango"
    verbose_name = "Native async Django"

    def ready(self) -> None:
        from django.db.backends.signals import connection_created

        from . import (
            checks,  # noqa: F401  (registers system checks)
            conf,
            middleware,
            signals,
        )
        from .db import patch, scope, session

        scope.install_connection_hook()
        connection_created.connect(session.install)
        signals.install()
        signals.install_async_to_sync()
        middleware.install()
        if conf.get("PATCH_ORM"):
            patch.install()
