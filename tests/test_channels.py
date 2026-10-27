"""Django Channels: NativeASGIHandler behind a ProtocolTypeRouter.

The application mirrors a typical Channels project: HTTP and lifespan go to
NativeASGIHandler, websockets go through an authentication middleware that
looks the user up with the async ORM, as the CRM's JWTWebSocketMiddleware does.
"""

import json
import threading
import unittest
from urllib.parse import parse_qs

from asgiref.testing import ApplicationCommunicator
from django.db import transaction
from django.urls import path

from asyncdjango.asgi import NativeASGIHandler
from asyncdjango.db import pool_stats
from asyncdjango.test import ASGIClient, NativeTransactionTestCase

from .testapp.models import Organization

try:
    from channels.generic.websocket import AsyncJsonWebsocketConsumer
    from channels.routing import ProtocolTypeRouter, URLRouter
except ImportError:  # pragma: no cover - channels is a test-only dependency
    AsyncJsonWebsocketConsumer = None


if AsyncJsonWebsocketConsumer is not None:

    class OrganizationMiddleware:
        """Resolve ``?org=<name>`` with the async ORM before the consumer runs."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            name = parse_qs(scope["query_string"].decode()).get("org", [None])[0]
            org = await Organization.objects.filter(name=name).afirst() if name else None
            return await self.inner({**scope, "organization": org}, receive, send)

    class OrganizationConsumer(AsyncJsonWebsocketConsumer):
        async def connect(self):
            if self.scope["organization"] is None:
                await self.close()
                return
            await self.accept()
            await self.send_json({
                "organization": self.scope["organization"].name,
                "count": await Organization.objects.acount(),
                "thread": threading.get_ident(),
            })

        async def receive_json(self, content):
            async with transaction.atomic():
                org = await Organization.objects.acreate(name=content["name"])
            await self.send_json({"created": org.name})


class Websocket(ApplicationCommunicator):
    # channels.testing imports daphne; this is the part of its
    # WebsocketCommunicator these tests need.
    def __init__(self, application, path):
        path, _, query = path.partition("?")
        super().__init__(application, {
            "type": "websocket", "path": path, "raw_path": path.encode(),
            "query_string": query.encode(), "headers": [], "subprotocols": [],
            "asgi": {"version": "3.0"},
        })

    async def connect(self):
        await self.send_input({"type": "websocket.connect"})
        return (await self.receive_output(5))["type"] == "websocket.accept"

    async def send_json(self, data):
        await self.send_input({"type": "websocket.receive", "text": json.dumps(data)})

    async def receive_json(self):
        message = await self.receive_output(5)
        self.testcase.assertEqual(message["type"], "websocket.send")
        return json.loads(message["text"])

    async def disconnect(self):
        await self.send_input({"type": "websocket.disconnect", "code": 1000})
        await self.wait(5)


@unittest.skipIf(AsyncJsonWebsocketConsumer is None, "channels is not installed")
class ChannelsTests(NativeTransactionTestCase):
    def application(self):
        handler = NativeASGIHandler()
        return ProtocolTypeRouter({
            "http": handler,
            # ProtocolTypeRouter rejects scope types it has no entry for, so
            # route lifespan explicitly or the pools are never opened or closed.
            "lifespan": handler,
            "websocket": OrganizationMiddleware(URLRouter([
                path("ws/organizations/", OrganizationConsumer.as_asgi()),
            ])),
        })

    def websocket(self, path):
        socket = Websocket(self.application(), path)
        socket.testcase = self
        return socket

    async def test_lifespan_opens_and_closes_pools(self):
        communicator = ApplicationCommunicator(
            self.application(), {"type": "lifespan", "asgi": {"version": "3.0"}})
        await communicator.send_input({"type": "lifespan.startup"})
        self.assertEqual(await communicator.receive_output(5),
                         {"type": "lifespan.startup.complete"})
        self.assertIn("default", pool_stats())
        await communicator.send_input({"type": "lifespan.shutdown"})
        self.assertEqual(await communicator.receive_output(5),
                         {"type": "lifespan.shutdown.complete"})
        self.assertEqual(pool_stats(), {})
        await communicator.wait(5)

    async def test_http_is_served_natively(self):
        await Organization.objects.acreate(name="Acme")
        response = await ASGIClient(self.application()).get("/orgs/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["names"], ["Acme"])
        self.assertEqual(response.json()["thread"], threading.get_ident())

    async def test_websocket_consumer_uses_the_native_orm(self):
        await Organization.objects.acreate(name="Acme")
        socket = self.websocket("/ws/organizations/?org=Acme")
        self.assertTrue(await socket.connect())
        self.assertEqual(await socket.receive_json(), {
            "organization": "Acme", "count": 1, "thread": threading.get_ident(),
        })
        await socket.send_json({"name": "Globex"})
        self.assertEqual(await socket.receive_json(), {"created": "Globex"})
        await socket.disconnect()
        self.assertTrue(await Organization.objects.filter(name="Globex").aexists())

    async def test_websocket_rejected_without_organization(self):
        socket = self.websocket("/ws/organizations/?org=Nobody")
        self.assertFalse(await socket.connect())
        await socket.disconnect()
