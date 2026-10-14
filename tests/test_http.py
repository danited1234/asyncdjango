"""HTTP routing, serializer, and ASGI behavior."""

import asyncio
import importlib.util
import json
import os
import unittest

from asyncdjango.asgi import ASGIApplication, JSONResponse
from asyncdjango.db import models
from asyncdjango.serializers import ModelSerializer
from asyncdjango.rest import ModelViewSet, Router


class User(models.Model):
    email = models.CharField(max_length=255)
    age = models.IntegerField(null=True)

    class Meta:
        db_table = "asyncdjango_http_test_users"


class UserSerializer(ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "age")


class UserViewSet(ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer


class SerializerTests(unittest.TestCase):
    def test_validation_and_representation(self) -> None:
        serializer = UserSerializer(data={"email": "a@example.com", "age": 20})
        self.assertTrue(serializer.is_valid())
        self.assertEqual(serializer.validated_data, {"email": "a@example.com", "age": 20})
        bad = UserSerializer(data={"email": 3})
        self.assertFalse(bad.is_valid())
        self.assertIn("email", bad.errors)
        read_only = UserSerializer(data={"id": 99, "email": "a@example.com"})
        self.assertFalse(read_only.is_valid())
        self.assertIn("id", read_only.errors)
        self.assertEqual(
            UserSerializer(User(id=1, email="a@example.com", age=20)).data,
            {"id": 1, "email": "a@example.com", "age": 20},
        )


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_route_and_method_errors(self) -> None:
        class FakeDatabase:
            async def connect(self):
                pass

            async def close(self):
                pass

        app = ASGIApplication(FakeDatabase())

        async def ping(request):
            return JSONResponse({"ok": True})

        app.add_route("/ping", ping)

        for method, path, expected in (
            ("GET", "/ping", 200), ("POST", "/ping", 405),
            ("GET", "/missing", 404),
        ):
            sent = []
            inbox = asyncio.Queue()
            inbox.put_nowait({"type": "http.request", "body": b""})

            async def send_one(message):
                sent.append(message)

            await app({"type": "http", "method": method, "path": path},
                      inbox.get, send_one)
            self.assertEqual(sent[0]["status"], expected)


HAS_DATABASE = bool(os.environ.get("ASYNCDJANGO_DATABASE_URL"))
HAS_DRIVER = importlib.util.find_spec("asyncpg") is not None


@unittest.skipUnless(HAS_DATABASE and HAS_DRIVER, "PostgreSQL URL and asyncpg required")
class HTTPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from asyncdjango.db import AsyncPostgres
        from asyncdjango.db.migrations import apply_migration, initial_migration

        self.database = AsyncPostgres(os.environ["ASYNCDJANGO_DATABASE_URL"])
        await self.database.connect()
        self.migration = initial_migration("test_0002_http", [User])
        await apply_migration(self.database, self.migration)

    async def asyncTearDown(self) -> None:
        await self.database.execute('DROP TABLE IF EXISTS "asyncdjango_http_test_users"')
        await self.database.execute(
            'DELETE FROM "_asyncdjango_migrations" WHERE "name" = $1',
            self.migration.name,
        )
        await self.database.close()

    async def _call_http(self, app, method, path, body=b""):
        inbox = asyncio.Queue()
        inbox.put_nowait({"type": "http.request", "body": body})
        sent = []

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": method, "path": path},
                  inbox.get, send)
        return sent

    async def test_model_to_serializer_to_asgi_response(self) -> None:
        app = ASGIApplication(self.database)

        async def create_user(request):
            serializer = UserSerializer(data=await request.json())
            if not serializer.is_valid():
                return JSONResponse(serializer.errors, 400)
            await serializer.asave()
            return JSONResponse(serializer.data, 201)

        app.add_route("/users", create_user, methods=("POST",))
        sent = await self._call_http(
            app, "POST", "/users",
            json.dumps({"email": "api@example.com", "age": 21}).encode(),
        )
        self.assertEqual(sent[0]["status"], 201)
        payload = json.loads(sent[1]["body"])
        self.assertEqual(payload["email"], "api@example.com")
        self.assertIsInstance(payload["id"], int)
        self.assertEqual(await User.objects.using(self.database).count(), 1)

    async def test_viewset_router_crud(self) -> None:
        app = ASGIApplication(self.database)
        Router(app).register("users", UserViewSet)

        created = await self._call_http(
            app, "POST", "/users",
            json.dumps({"email": "viewset@example.com", "age": 21}).encode(),
        )
        self.assertEqual(created[0]["status"], 201)
        user_id = json.loads(created[1]["body"])["id"]

        listed = await self._call_http(app, "GET", "/users")
        self.assertEqual(len(json.loads(listed[1]["body"])), 1)
        detail = await self._call_http(app, "GET", f"/users/{user_id}")
        self.assertEqual(json.loads(detail[1]["body"])["email"], "viewset@example.com")

        updated = await self._call_http(
            app, "PATCH", f"/users/{user_id}", b'{"age":22}'
        )
        self.assertEqual(json.loads(updated[1]["body"])["age"], 22)
        rejected = await self._call_http(app, "POST", "/users", b'{"email":2}')
        self.assertEqual(rejected[0]["status"], 400)
        deleted = await self._call_http(app, "DELETE", f"/users/{user_id}")
        self.assertEqual(deleted[0]["status"], 204)
        self.assertEqual(deleted[1]["body"], b"")
        missing = await self._call_http(app, "GET", f"/users/{user_id}")
        self.assertEqual(missing[0]["status"], 404)

    async def test_disconnect_cancels_database_transaction(self) -> None:
        from asyncdjango.db import transaction

        started = asyncio.Event()
        app = ASGIApplication(self.database)

        async def slow(request):
            async with transaction.atomic() as connection:
                started.set()
                await connection.fetch("SELECT pg_sleep(10)")
            return JSONResponse({"ok": True})

        app.add_route("/slow", slow)
        inbox = asyncio.Queue()
        inbox.put_nowait({"type": "http.request", "body": b""})
        sent = []

        async def send(message):
            sent.append(message)

        call = asyncio.create_task(app(
            {"type": "http", "method": "GET", "path": "/slow"},
            inbox.get, send,
        ))
        await asyncio.wait_for(started.wait(), 3)
        inbox.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(call, 3)
        self.assertEqual(sent, [])
        rows = await asyncio.wait_for(self.database.fetch("SELECT 1 AS n"), 3)
        self.assertEqual(rows[0]["n"], 1)
