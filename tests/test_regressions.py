"""Regressions for issues found in review: scope ownership, contexts, pools."""

import asyncio
import contextvars
import gc

from django.db.models.signals import post_save
from django.test import override_settings
from django.utils import translation

from asyncdjango import sync_to_native
from asyncdjango.db import await_, close_pools, transaction

from .test_orm import OffloadGuardedTestCase
from .testapp.models import AuditLog, Employee, Organization, Tag


class RegressionTests(OffloadGuardedTestCase):
    async def names(self):
        return [org.name async for org in Organization.objects.order_by("name")]

    async def test_abandoned_generator_transaction_does_not_capture_later_writes(self):
        async def rows():
            async with transaction.atomic():
                await Organization.objects.acreate(name="in-generator")
                yield 1

        with self.assertLogs("asyncdjango.transaction", "WARNING"):
            async for _ in rows():
                break
            # The generator is suspended inside its atomic block; this write
            # is outside it and must commit on its own.
            await Organization.objects.acreate(name="after")
            gc.collect()
            await asyncio.sleep(0.2)  # the loop finalizes the generator
        self.assertEqual(await self.names(), ["after"])
        await self.assert_pool_idle()

    async def test_async_signal_receiver_that_queries(self):
        async def audit(sender, instance, created, **kwargs):
            await AuditLog.objects.acreate(message=f"tag {instance.name}")

        post_save.connect(audit, sender=Tag)
        try:
            await Tag.objects.acreate(name="outside")
            async with transaction.atomic():
                await Tag.objects.acreate(name="inside")
        finally:
            post_save.disconnect(audit, sender=Tag)
        self.assertEqual(sorted([log.message async for log in AuditLog.objects.all()]),
                         ["tag inside", "tag outside"])

    async def test_one_atomic_instance_shared_by_concurrent_tasks(self):
        shared = transaction.atomic()

        async def work(name, delay):
            async with shared:
                await Organization.objects.acreate(name=name)
                await asyncio.sleep(delay)

        await asyncio.gather(work("a", 0.2), work("b", 0))
        self.assertEqual(await self.names(), ["a", "b"])
        await self.assert_pool_idle()

    async def test_aiterator_break_without_aclose(self):
        org = await self.make_org()
        await Employee.objects.abulk_create(
            Employee(organization=org, email=f"{n}@example.com") for n in range(30))
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        try:
            async for _ in Employee.objects.aiterator(chunk_size=10):
                break
            gc.collect()
            await asyncio.sleep(0.2)
        finally:
            loop.set_exception_handler(None)
        self.assertEqual(errors, [])
        await self.assert_pool_idle()

    async def test_aiterator_per_row_queries_share_its_connection(self):
        await close_pools()
        with override_settings(ASYNCDJANGO={"POOL": {"min_size": 1, "max_size": 1,
                                                      "timeout": 2}}):
            org = await self.make_org("Acme", employees=3)
            seen = 0
            async for employee in Employee.objects.aiterator(chunk_size=2):
                self.assertTrue(await Employee.objects.filter(pk=employee.pk).aexists())
                seen += 1
            self.assertEqual(seen, 3)
            self.assertEqual(org.name, "Acme")
            await close_pools()

    async def test_context_variables_flow_like_sync_to_async(self):
        await sync_to_native(translation.activate)("fr")
        try:
            self.assertEqual(translation.get_language(), "fr")
        finally:
            translation.deactivate()
        marker = contextvars.ContextVar("marker", default="unset")

        async def read():
            return marker.get()

        def set_then_await():
            marker.set("from sync code")
            return await_(read())

        self.assertEqual(await sync_to_native(set_then_await)(), "from sync code")
        self.assertEqual(marker.get(), "from sync code")

    async def test_release_after_pools_were_replaced(self):
        async with transaction.atomic():
            await Organization.objects.acreate(name="kept")
            await close_pools()
        self.assertEqual(await self.names(), ["kept"])
