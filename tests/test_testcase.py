"""asyncdjango.test.NativeTestCase isolates tests with a rolled-back transaction."""

import asyncio

from asyncdjango.db import transaction
from asyncdjango.test import NativeTestCase

from .testapp.models import AuditLog, Organization


class RollbackIsolationTests(NativeTestCase):
    async def asyncSetUp(self):
        self.org = await Organization.objects.acreate(name="fixture")

    async def test_a_writes_are_visible_inside_the_test(self):
        await Organization.objects.acreate(name="a")
        self.assertEqual(await Organization.objects.acount(), 2)

    async def test_b_previous_test_was_rolled_back(self):
        self.assertEqual([o.name async for o in Organization.objects.all()], ["fixture"])

    async def test_durable_atomic_and_gather_share_the_test_transaction(self):
        async with transaction.atomic(durable=True):
            await Organization.objects.acreate(name="durable")
        counts = await asyncio.gather(Organization.objects.acount(), AuditLog.objects.acount())
        self.assertEqual(counts, [2, 0])
