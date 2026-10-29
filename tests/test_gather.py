"""asyncdjango.gather(): concurrent ORM calls within one request."""

import time
import warnings

from django.db import transaction

from asyncdjango import gather
from asyncdjango.exceptions import CrossTaskConnectionError

from .test_orm import OffloadGuardedTestCase
from .testapp.models import Organization


def slow(seconds):
    return Organization.objects.extra(select={"slept": f"pg_sleep({seconds})"}).afirst()


class GatherTests(OffloadGuardedTestCase):
    async def test_results_in_order(self):
        acme = await self.make_org("Acme", employees=2)
        await self.make_org("Globex")
        org, names, count = await gather(
            Organization.objects.aget(name="Acme"),
            Organization.objects.order_by("name").values_list("name", flat=True),
            acme.employees.acount(),
        )
        self.assertEqual((org, names, count), (acme, ["Acme", "Globex"], 2))
        self.assertEqual(await gather(), [])

    async def test_runs_concurrently_up_to_limit(self):
        await self.make_org()
        started = time.monotonic()
        await gather(*(slow(0.3) for _ in range(4)))
        self.assertLess(time.monotonic() - started, 0.9)

        started = time.monotonic()
        await gather(*(slow(0.3) for _ in range(4)), limit=2)
        self.assertGreaterEqual(time.monotonic() - started, 0.6)
        await self.assert_pool_idle()

    async def test_failure_cancels_the_rest_and_returns_connections(self):
        self.allow_cancel_thread()
        await self.make_org()
        started = time.monotonic()
        with self.assertRaises(Organization.DoesNotExist):
            await gather(slow(5), Organization.objects.aget(name="missing"))
        self.assertLess(time.monotonic() - started, 3)
        await self.assert_pool_idle()

    async def test_refused_inside_a_transaction(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)  # "never awaited"
            async with transaction.atomic():
                with self.assertRaises(CrossTaskConnectionError):
                    await gather(Organization.objects.acount())
                with self.assertRaises(ValueError):
                    await gather(Organization.objects.acount(), limit=0)
