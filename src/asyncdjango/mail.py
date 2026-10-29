"""Send email from native code without blocking the event loop.

    EMAIL_BACKEND = "asyncdjango.mail.EmailBackend"

Django's SMTP backend talks to the server with ``smtplib``. Inside native code
this backend gives it an ``smtplib.SMTP`` look-alike whose calls are awaited
through ``aiosmtplib`` instead, so ``send_mail()``, ``EmailMessage.send()``
and ``mail_admins()`` from an async view (via ``asend_mail()`` or
``sync_to_native``), a signal receiver, or a ``save()`` override wait on the
loop. Elsewhere (Celery, management commands, sync views) it is Django's SMTP
backend unchanged.
"""

from __future__ import annotations

import smtplib
from typing import Any

import aiosmtplib
from django.conf import settings
from django.core.mail import send_mail
from django.core.mail.backends import smtp

from ._threads import run_blocking
from .db.bridge import await_, in_native_context
from .db.scope import run_native


def _call(awaitable: Any) -> Any:
    # Raise smtplib's exceptions, which Django's backend catches.
    try:
        return await_(awaitable)
    except aiosmtplib.SMTPServerDisconnected as exc:
        raise smtplib.SMTPServerDisconnected(str(exc)) from exc
    except aiosmtplib.SMTPRecipientsRefused as exc:
        refused = {error.recipient: (error.code, error.message.encode())
                   for error in exc.recipients}
        raise smtplib.SMTPRecipientsRefused(refused) from exc
    except aiosmtplib.SMTPResponseException as exc:
        raise smtplib.SMTPResponseException(exc.code, exc.message) from exc
    except aiosmtplib.SMTPConnectError:
        raise  # An OSError, as smtplib's connection errors are.
    except aiosmtplib.SMTPException as exc:
        raise smtplib.SMTPException(str(exc)) from exc


class NativeSMTP:
    """The part of ``smtplib.SMTP`` that Django's backend uses, on aiosmtplib."""

    def __init__(self, host: str, port: int, *, local_hostname: str | None = None,
                 timeout: float | None = None, context: Any = None) -> None:
        # Django passes an SSL context only for EMAIL_USE_SSL.
        self.client = aiosmtplib.SMTP(
            hostname=host, port=port, local_hostname=local_hostname, timeout=timeout,
            use_tls=context is not None, tls_context=context, start_tls=False)
        _call(self.client.connect())

    def starttls(self, context: Any = None) -> Any:
        return _call(self.client.starttls(tls_context=context))

    def login(self, user: str, password: str) -> Any:
        return _call(self.client.login(user, password))

    def sendmail(self, from_addr: str, to_addrs: list[str], msg: bytes) -> dict[str, Any]:
        errors, _ = _call(self.client.sendmail(from_addr, to_addrs, msg))
        return errors

    def quit(self) -> Any:
        return _call(self.client.quit())

    def close(self) -> None:
        self.client.close()


class EmailBackend(smtp.EmailBackend):
    @property
    def connection_class(self) -> Any:
        if in_native_context():
            return NativeSMTP
        return super().connection_class


_NON_BLOCKING = {"asyncdjango.mail.EmailBackend",
                 "django.core.mail.backends.locmem.EmailBackend",
                 "django.core.mail.backends.dummy.EmailBackend"}


async def asend_mail(*args: Any, **kwargs: Any) -> int:
    """``django.core.mail.send_mail()`` for async code.

    Native with this backend (and Django's in-memory test backends); any
    other ``EMAIL_BACKEND`` runs in a worker thread, as it would in Django.
    """
    backend = kwargs.get("connection")
    if backend is not None:
        path = f"{type(backend).__module__}.{type(backend).__qualname__}"
    else:
        path = settings.EMAIL_BACKEND
    if path in _NON_BLOCKING:
        return await run_native(send_mail, *args, **kwargs)
    return await run_blocking(send_mail, *args, **kwargs)
