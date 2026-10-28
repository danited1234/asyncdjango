"""NativeASGIHandler: Django's request path without worker threads."""

import asyncio
import threading

from django.db import connections
from django.test import override_settings

from asyncdjango.asgi import NativeASGIHandler
from asyncdjango.db import pool_stats
from asyncdjango.test import ASGIClient

from .test_orm import OffloadGuardedTestCase
from .testapp import views
from .testapp.models import Organization


class HandlerTests(OffloadGuardedTestCase):
    def client(self):
        return ASGIClient(NativeASGIHandler())

    async def test_async_view_runs_on_the_loop_thread(self):
        await Organization.objects.acreate(name="Acme")
        response = await self.client().get("/orgs/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["names"], ["Acme"])
        self.assertEqual(body["thread"], threading.get_ident())
        self.assertEqual(self.offload.calls, [])

    async def test_deferred_render_and_404_are_native(self):
        await Organization.objects.acreate(name="Acme")
        response = await self.client().get("/deferred/")
        self.assertEqual((response.status_code, response.content), (200, b"1"))
        response = await self.client().get("/missing/")
        self.assertEqual(response.status_code, 404)

    @override_settings(
        MIDDLEWARE=["tests.testapp.middleware.SyncCountingMiddleware"],
        ASYNCDJANGO={"NATIVE_SYNC_MIDDLEWARE": [
            "tests.testapp.middleware.SyncCountingMiddleware"]},
    )
    async def test_listed_sync_middleware_runs_natively(self):
        await Organization.objects.acreate(name="Acme")
        response = await self.client().get("/orgs/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-org-count"], "1")
        body = response.json()
        self.assertEqual(body["middleware_thread"], threading.get_ident())
        self.assertEqual(body["org_count"], 1)

    @override_settings(MIDDLEWARE=["tests.testapp.middleware.SyncCountingMiddleware"])
    async def test_unlisted_sync_middleware_keeps_djangos_thread(self):
        self.offload.forbid = False
        response = await self.client().get("/orgs/")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["middleware_thread"], threading.get_ident())

    async def test_atomic_requests_for_async_views(self):
        # The 500 is reported from a thread on purpose (asyncdjango._threads).
        self.offload.allow = self.offload.allow | {"response_for_exception", "log_response"}
        connections.settings["default"]["ATOMIC_REQUESTS"] = True
        try:
            client = self.client()
            ok = await client.get("/create/", query_string="name=kept")
            self.assertEqual(ok.status_code, 200)
            failed = await client.get("/create/", query_string="name=lost&fail=1")
            self.assertEqual(failed.status_code, 500)
        finally:
            connections.settings["default"]["ATOMIC_REQUESTS"] = False
        self.assertEqual([o.name async for o in Organization.objects.all()], ["kept"])

    async def test_client_disconnect_cancels_view_and_rolls_back(self):
        self.allow_cancel_thread()
        views.events.clear()
        app = NativeASGIHandler()
        disconnect = asyncio.Event()
        messages = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            if messages:
                return messages.pop()
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        scope = {"type": "http", "method": "GET", "path": "/slow/", "query_string": b"",
                 "headers": [(b"host", b"testserver")], "root_path": ""}
        request = asyncio.create_task(app(scope, receive, send))
        while "created" not in views.events:
            await asyncio.sleep(0.05)
        disconnect.set()
        await asyncio.wait_for(request, 5)
        self.assertEqual(views.events, ["created", "cancelled"])
        await asyncio.sleep(0.1)
        self.assertEqual(await Organization.objects.acount(), 0)
        await self.assert_pool_idle()

    async def test_lifespan_opens_and_closes_pools(self):
        app = NativeASGIHandler()
        incoming = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        sent = []
        opened = asyncio.Event()

        async def receive():
            if len(incoming) == 1:
                await opened.wait()
            return incoming.pop(0)

        async def send(message):
            sent.append(message["type"])
            if message["type"] == "lifespan.startup.complete":
                self.assertIn("default", pool_stats())
                opened.set()

        await app({"type": "lifespan"}, receive, send)
        self.assertEqual(sent, ["lifespan.startup.complete", "lifespan.shutdown.complete"])
        self.assertEqual(pool_stats(), {})
