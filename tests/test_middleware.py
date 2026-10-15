"""Custom and built-in middleware on NativeASGIHandler."""

import threading

from django.core import checks
from django.test import override_settings

from asyncdjango.asgi import NativeASGIHandler
from asyncdjango.test import ASGIClient

from .test_orm import OffloadGuardedTestCase
from .testapp.models import Organization

BUILTINS = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.http.ConditionalGetMiddleware",
    "django.middleware.gzip.GZipMiddleware",
]
MIXIN = "tests.testapp.middleware.MixinHeaderMiddleware"


class MiddlewareTests(OffloadGuardedTestCase):
    async def get(self, path="/orgs/", **kwargs):
        return await ASGIClient(NativeASGIHandler()).get(path, **kwargs)

    @override_settings(MIDDLEWARE=BUILTINS)
    async def test_django_builtin_middleware_runs_without_threads(self):
        response = await self.get(headers={"accept-encoding": "gzip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.offload.calls, [])

    @override_settings(MIDDLEWARE=[
        "tests.testapp.middleware.DualModeMiddleware",
        "tests.testapp.middleware.async_function_middleware",
    ])
    async def test_custom_async_middleware_awaits_on_the_loop(self):
        await Organization.objects.acreate(name="Acme")
        response = await self.get()
        self.assertEqual(response.headers["x-async-function"], "Acme")
        self.assertEqual(response.headers["x-dual-mode"], "async")
        self.assertEqual(response.json()["middleware_thread"], threading.get_ident())
        self.assertEqual(self.offload.calls, [])

    @override_settings(MIDDLEWARE=[MIXIN])
    async def test_unlisted_mixin_middleware_keeps_djangos_thread(self):
        self.offload.forbid = False
        response = await self.get()
        self.assertEqual(response.headers["x-mixin-count"], "0")
        self.assertTrue(self.offload.calls)

    @override_settings(MIDDLEWARE=[MIXIN], ASYNCDJANGO={"NATIVE_SYNC_MIDDLEWARE": [MIXIN]})
    async def test_listed_mixin_middleware_runs_natively(self):
        await Organization.objects.acreate(name="Acme")
        response = await self.get()
        self.assertEqual(response.headers["x-mixin-count"], "1")
        self.assertEqual(self.offload.calls, [])

    @override_settings(MIDDLEWARE=["tests.testapp.middleware.AsyncExceptionMiddleware"])
    async def test_async_process_exception_runs_natively(self):
        response = await self.get("/create/", query_string="name=x&fail=1")
        self.assertEqual(response.status_code, 418)
        self.assertEqual(response.json(), {"handled": "boom", "organizations": 1})
        self.assertEqual(self.offload.calls, [])

    def test_checks_report_thread_bound_mixin_middleware(self):
        with override_settings(MIDDLEWARE=[MIXIN, *BUILTINS]):
            ids = [(m.id, m.msg.split("'")[1]) for m in checks.run_checks()
                   if m.id in {"asyncdjango.W002", "asyncdjango.W003"}]
        self.assertEqual(ids, [("asyncdjango.W003", MIXIN)])
