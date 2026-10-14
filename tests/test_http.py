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
from asyncdjango.filters import ExactFilterBackend, OrderingFilter
from asyncdjango.pagination import PageNumberPagination
from asyncdjango.permissions import BasePermission


class User(models.Model):
    email = models.CharField(max_length=255, unique=True)
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


class SmallPages(PageNumberPagination):
    page_size = 2
    max_page_size = 3


class PagedUserViewSet(UserViewSet):
    filter_backends = (ExactFilterBackend, OrderingFilter)
    filterset_fields = ("age",)
    ordering_fields = ("id", "age")
    pagination_class = SmallPages


class YoungOnlyObjectPermission(BasePermission):
    async def has_object_permission(self, request, view, obj):
        return obj.age != 30


class RestrictedUserViewSet(UserViewSet):
    permission_classes = (YoungOnlyObjectPermission,)


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

    async def _call_http(self, app, method, path, body=b"", query_string=b""):
        inbox = asyncio.Queue()
        inbox.put_nowait({"type": "http.request", "body": body})
        sent = []

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": method, "path": path,
                   "query_string": query_string},
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
        duplicate = await self._call_http(
            app, "POST", "/users", b'{"email":"viewset@example.com"}'
        )
        self.assertEqual(duplicate[0]["status"], 400)
        self.assertIn("email", json.loads(duplicate[1]["body"]))
        same_email_update = await self._call_http(
            app, "PATCH", f"/users/{user_id}", b'{"email":"viewset@example.com"}'
        )
        self.assertEqual(same_email_update[0]["status"], 200)
        deleted = await self._call_http(app, "DELETE", f"/users/{user_id}")
        self.assertEqual(deleted[0]["status"], 204)
        self.assertEqual(deleted[1]["body"], b"")
        missing = await self._call_http(app, "GET", f"/users/{user_id}")
        self.assertEqual(missing[0]["status"], 404)

    async def test_concurrent_duplicate_writes_are_rejected(self) -> None:
        app = ASGIApplication(self.database)
        Router(app).register("users", UserViewSet)
        payload = b'{"email":"race@example.com"}'
        first, second = await asyncio.gather(
            self._call_http(app, "POST", "/users", payload),
            self._call_http(app, "POST", "/users", payload),
        )
        self.assertEqual(sorted([first[0]["status"], second[0]["status"]]),
                         [201, 400])
        self.assertEqual(await User.objects.using(self.database).count(), 1)

    async def test_allowlisted_filter_order_and_page(self) -> None:
        for index, age in enumerate((20, 30, 20, 30, 20)):
            await User.objects.using(self.database).create(
                email=f"page{index}@example.com", age=age
            )
        app = ASGIApplication(self.database)
        Router(app).register("users", PagedUserViewSet)
        first = await self._call_http(
            app, "GET", "/users", query_string=b"age=20&ordering=-id&page=1"
        )
        payload = json.loads(first[1]["body"])
        self.assertEqual(first[0]["status"], 200)
        self.assertEqual(payload["count"], 3)
        self.assertEqual([row["email"] for row in payload["results"]],
                         ["page4@example.com", "page2@example.com"])
        self.assertIn("page=2", payload["next"])
        second = await self._call_http(
            app, "GET", "/users", query_string=b"age=20&ordering=-id&page=2"
        )
        self.assertEqual(json.loads(second[1]["body"])["results"][0]["email"],
                         "page0@example.com")
        bad = await self._call_http(app, "GET", "/users",
                                    query_string=b"ordering=email")
        self.assertEqual(bad[0]["status"], 400)
        bad_page = await self._call_http(app, "GET", "/users",
                                         query_string=b"page_size=1000")
        self.assertEqual(bad_page[0]["status"], 400)

    async def test_object_permissions_apply_to_detail_writes(self) -> None:
        user = await User.objects.using(self.database).create(
            email="restricted@example.com", age=30
        )
        app = ASGIApplication(self.database)
        Router(app).register("users", RestrictedUserViewSet)
        for method, body in (("GET", b""), ("PATCH", b'{"age":20}'),
                             ("DELETE", b"")):
            sent = await self._call_http(app, method, f"/users/{user.pk}", body)
            self.assertEqual(sent[0]["status"], 403)
        self.assertEqual(await User.objects.using(self.database).count(), 1)

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
