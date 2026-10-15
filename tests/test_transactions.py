"""async with transaction.atomic() on the native backend."""

import asyncio
import warnings

from django.db import IntegrityError
from django.db import transaction as django_transaction
from django.db.transaction import TransactionManagementError

from asyncdjango.db import pool_stats, transaction
from asyncdjango.db.patch import SyncFallbackWarning
from asyncdjango.exceptions import CrossTaskConnectionError, NativeAsyncUnsupported
from asyncdjango.test import NativeTransactionTestCase
from django.test import override_settings

from .test_orm import OffloadGuardedTestCase
from .testapp.models import Organization, SqliteNote


class AtomicTests(OffloadGuardedTestCase):
    async def names(self):
        return [org.name async for org in Organization.objects.order_by("name")]

    async def test_commit_and_rollback_on_exception(self):
        async with transaction.atomic():
            await Organization.objects.acreate(name="kept")
        with self.assertRaises(ZeroDivisionError):
            async with transaction.atomic():
                await Organization.objects.acreate(name="lost")
                1 / 0
        self.assertEqual(await self.names(), ["kept"])

    async def test_other_connections_do_not_see_uncommitted_rows(self):
        async with transaction.atomic():
            await Organization.objects.acreate(name="pending")
            # A separate task uses its own pooled connection.
            outside = await asyncio.create_task(self.count_outside())
            inside = await Organization.objects.acount()
        self.assertEqual((inside, outside), (1, 0))

    async def count_outside(self):
        # Child tasks inherit the context; reset the scope deliberately.
        from asyncdjango.db.scope import _current_scope

        _current_scope.set(None)
        return await Organization.objects.acount()

    async def test_nested_savepoint_rollback(self):
        async with transaction.atomic():
            await Organization.objects.acreate(name="outer")
            try:
                async with transaction.atomic():
                    await Organization.objects.acreate(name="inner")
                    raise ValueError
            except ValueError:
                pass
            # IntegrityError in a savepoint leaves the outer block usable.
            try:
                async with transaction.atomic():
                    await Organization.objects.acreate(name="outer")
            except IntegrityError:
                pass
            await Organization.objects.acreate(name="after")
        self.assertEqual(await self.names(), ["after", "outer"])

    async def test_error_without_savepoint_marks_block_broken(self):
        with self.assertRaises(TransactionManagementError):
            async with transaction.atomic():
                await Organization.objects.acreate(name="a")
                try:
                    await Organization.objects.acreate(name="a")
                except IntegrityError:
                    pass
                await Organization.objects.acount()
        self.assertEqual(await self.names(), [])

    async def test_set_rollback_and_durable(self):
        async with transaction.atomic():
            await Organization.objects.acreate(name="discarded")
            django_transaction.set_rollback(True)
        self.assertEqual(await self.names(), [])
        with self.assertRaisesRegex(RuntimeError, "durable"):
            async with transaction.atomic():
                async with transaction.atomic(durable=True):
                    pass

    async def test_select_for_update_requires_transaction(self):
        org = await Organization.objects.acreate(name="locked")
        with self.assertRaises(TransactionManagementError):
            await Organization.objects.select_for_update().aget(pk=org.pk)
        async with transaction.atomic():
            locked = await Organization.objects.select_for_update().aget(pk=org.pk)
            locked.name = "updated"
            await locked.asave()
        self.assertEqual(await self.names(), ["updated"])

    async def test_select_for_update_blocks_concurrent_writer(self):
        org = await Organization.objects.acreate(name="v0")
        order = []

        async def writer(name, delay):
            async with transaction.atomic():
                row = await Organization.objects.select_for_update().aget(pk=org.pk)
                order.append(f"locked {name} saw {row.name}")
                await asyncio.sleep(delay)
                row.name = name
                await row.asave()

        await asyncio.gather(writer("first", 0.3), self.start_later(writer("second", 0)))
        self.assertEqual(order, ["locked first saw v0", "locked second saw first"])

    async def start_later(self, coroutine):
        await asyncio.sleep(0.1)
        await coroutine

    async def test_on_commit_sync_and_async(self):
        calls = []

        async def notify():
            await asyncio.sleep(0)
            calls.append(("async", await Organization.objects.acount()))

        async with transaction.atomic():
            await Organization.objects.acreate(name="x")
            transaction.on_commit(lambda: calls.append(("sync",)))
            transaction.on_commit(notify)
            self.assertEqual(calls, [])
        self.assertEqual(calls, [("sync",), ("async", 1)])
        calls.clear()
        async with transaction.atomic():
            # A lambda returning a coroutine is awaited too.
            transaction.on_commit(lambda: notify())
        self.assertEqual(calls, [("async", 1)])
        calls.clear()
        async with transaction.atomic():
            transaction.on_commit(notify)
            django_transaction.set_rollback(True)
        self.assertEqual(calls, [])

    async def test_decorator_and_djangos_atomic_with_async_with(self):
        @transaction.atomic
        async def create(name):
            await Organization.objects.acreate(name=name)
            if name == "bad":
                raise ValueError

        await create("good")
        with self.assertRaises(ValueError):
            await create("bad")

        @django_transaction.atomic(durable=True)
        async def via_django(name):
            await Organization.objects.acreate(name=name)

        await via_django("django")
        async with django_transaction.atomic():
            await Organization.objects.acreate(name="django-cm")
        self.assertEqual(await self.names(), ["django", "django-cm", "good"])

    async def test_child_tasks_cannot_share_transaction(self):
        async with transaction.atomic():
            with self.assertRaises(CrossTaskConnectionError):
                await asyncio.gather(Organization.objects.acount(),
                                     Organization.objects.acount())

    async def test_cancellation_rolls_back_and_releases(self):
        self.offload.allow = self.offload.allow | {"cancel"}
        started = asyncio.Event()

        async def work():
            async with transaction.atomic():
                await Organization.objects.acreate(name="cancelled")
                started.set()
                await Organization.objects.extra(select={"s": "pg_sleep(5)"}).afirst()

        task = asyncio.create_task(work())
        await started.wait()
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        self.assertEqual(await self.names(), [])
        await self.assert_pool_idle()


class ThreadInteropTests(NativeTransactionTestCase):
    async def test_sync_to_async_inside_atomic_uses_its_own_connection(self):
        from asgiref.sync import sync_to_async
        from django.db import connections

        async with transaction.atomic():
            await Organization.objects.acreate(name="pending")
            # The worker thread inherits the context but not the transaction.
            outside = await sync_to_async(Organization.objects.count)()
        self.assertEqual(outside, 0)
        self.assertEqual(await Organization.objects.acount(), 1)
        await sync_to_async(connections.close_all)()


class UnsupportedAliasTests(NativeTransactionTestCase):
    databases = {"default", "other"}

    async def test_non_postgres_alias_raises(self):
        with self.assertRaises(NativeAsyncUnsupported):
            await SqliteNote.objects.acount()
        with self.assertRaises(NativeAsyncUnsupported):
            async with transaction.atomic(using="other"):
                pass

    async def test_fallback_warns_and_uses_django(self):
        with override_settings(ASYNCDJANGO={"ALLOW_SYNC_FALLBACK": True}):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assertEqual(await SqliteNote.objects.acount(), 0)
        self.assertTrue(any(issubclass(w.category, SyncFallbackWarning) for w in caught))
