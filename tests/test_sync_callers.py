"""Async code called from synchronous code through ``async_to_sync()``.

This is how Celery tasks, management commands, and async tests on Django's
``TestCase`` reach async services. asgiref runs each such call on a new event
loop that lives for one call, while the calling thread waits. ORM calls there
run on that calling thread with its own Django connection, as in plain Django:
no pool is created per call, and the caller's open transaction is visible.
"""

import asyncio
import threading

from asgiref.sync import async_to_sync
from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase

from asyncdjango import sync_to_native
from asyncdjango.db import pool_stats
from asyncdjango.db import transaction as native_transaction

from .testapp.models import AuditLog, Employee, Organization, SqliteNote, Tag


class TestCaseCallerTests(TestCase):
    """Django's TestCase: each test's transaction is on the caller's connection."""

    databases = {"default", "other"}

    def test_orm_calls_see_the_callers_transaction(self):
        org = Organization.objects.create(name="Acme")  # not committed

        async def service():
            found = await Organization.objects.aget(pk=org.pk)
            employee = await Employee.objects.acreate(organization=found,
                                                      email="ann@example.com")
            await employee.tags.aadd(await Tag.objects.acreate(name="lead"))
            names = [o.name async for o in Organization.objects.all()]
            emails = [e.email async for e in Employee.objects.aiterator(chunk_size=1)]
            return names, emails, await employee.tags.acount(), pool_stats()

        names, emails, tag_count, pools = async_to_sync(service)()
        self.assertEqual((names, emails, tag_count), (["Acme"], ["ann@example.com"], 1))
        self.assertEqual(pools, {})  # no connection pool for the one-call loop
        # The post_save receiver ran on the same connection.
        self.assertTrue(AuditLog.objects.filter(message="hired ann@example.com").exists())

    def test_sync_to_native_runs_on_the_calling_thread(self):
        def work():
            return threading.get_ident(), Organization.objects.count()

        self.assertEqual(async_to_sync(sync_to_native(work))(),
                         (threading.get_ident(), 0))

    def test_async_atomic_commits_and_rolls_back(self):
        async def service():
            async with transaction.atomic():
                await Organization.objects.acreate(name="kept")
            try:
                async with transaction.atomic():
                    await Organization.objects.acreate(name="dropped")
                    raise ValueError
            except ValueError:
                pass

        async_to_sync(service)()
        self.assertEqual(list(Organization.objects.values_list("name", flat=True)),
                         ["kept"])

    def test_on_commit_registers_on_the_callers_transaction(self):
        calls = []

        async def notify():
            calls.append("async")

        async def service():
            async with transaction.atomic():
                await Organization.objects.acreate(name="Acme")
                native_transaction.on_commit(lambda: calls.append("sync"))
                native_transaction.on_commit(notify)

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            async_to_sync(service)()
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(calls, ["sync", "async"])

    def test_non_postgresql_alias_uses_djangos_implementation(self):
        async def service():
            await SqliteNote.objects.acreate(text="hi")
            return await SqliteNote.objects.acount()

        self.assertEqual(async_to_sync(service)(), 1)


class WorkerThreadTests(TransactionTestCase):
    def test_celery_style_task(self):
        # A Celery worker runs the task function in its own thread and closes
        # Django's connections afterwards (celery.fixups.django).
        result = {}

        def task():
            async def service():
                await Organization.objects.acreate(name="Acme")
                counts = await asyncio.gather(
                    Organization.objects.acount(),
                    Organization.objects.filter(name="Acme").acount(),
                )
                async with transaction.atomic():
                    org = await Organization.objects.select_for_update().aget(name="Acme")
                    org.name = "Acme Inc"
                    await org.asave()
                return counts, pool_stats()

            try:
                result["value"] = async_to_sync(service)()
            except BaseException as exc:
                result["error"] = exc
            finally:
                connections.close_all()

        worker = threading.Thread(target=task)
        worker.start()
        worker.join(timeout=30)
        self.assertNotIn("error", result)
        counts, pools = result["value"]
        self.assertEqual(counts, [1, 1])
        self.assertEqual(pools, {})
        self.assertTrue(Organization.objects.filter(name="Acme Inc").exists())
