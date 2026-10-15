"""Django's async ORM API running on the native backend."""

import asyncio
import datetime
import decimal
import threading

from django.core.exceptions import SynchronousOnlyOperation
from django.db import IntegrityError, connections
from django.db.models import Count, F, Prefetch, Q, Sum

from asyncdjango import sync_to_native
from asyncdjango.db import pool_stats
from asyncdjango.diagnostics import ThreadOffloadDetector
from asyncdjango.test import NativeTransactionTestCase

from .testapp.models import AuditLog, Employee, Note, Organization, Profile, Tag


class OffloadGuardedTestCase(NativeTransactionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Every test in this module must run without a worker thread.
        self.offload = ThreadOffloadDetector()
        await self.offload.__aenter__()

    async def asyncTearDown(self):
        await self.offload.__aexit__(None, None, None)
        await super().asyncTearDown()

    async def assert_pool_idle(self, alias="default"):
        """Every pooled connection is back (allowing for one still opening)."""
        for _ in range(50):
            stats = pool_stats()[alias]
            if stats["pool_size"] == stats["pool_available"]:
                return
            await asyncio.sleep(0.02)
        self.fail(f"connections still checked out: {stats}")

    async def make_org(self, name="Acme", employees=0):
        org = await Organization.objects.acreate(name=name)
        for number in range(employees):
            await Employee.objects.acreate(
                organization=org, email=f"{name.lower()}{number}@example.com",
                salary=decimal.Decimal(1000 + number),
            )
        return org


class QueryTests(OffloadGuardedTestCase):
    async def test_create_get_count_exists(self):
        org = await Organization.objects.acreate(name="Acme")
        self.assertIsNotNone(org.pk)
        self.assertIsNotNone(org.created_at.tzinfo)
        fetched = await Organization.objects.aget(pk=org.pk)
        self.assertEqual(fetched.name, "Acme")
        self.assertEqual(await Organization.objects.acount(), 1)
        self.assertTrue(await Organization.objects.filter(name="Acme").aexists())
        self.assertFalse(await Organization.objects.filter(name="Nope").aexists())
        with self.assertRaises(Organization.DoesNotExist):
            await Organization.objects.aget(name="Nope")
        await Organization.objects.acreate(name="Beta")
        with self.assertRaises(Organization.MultipleObjectsReturned):
            await Organization.objects.aget(name__in=["Acme", "Beta"])

    async def test_first_last_earliest_latest_in_bulk_contains(self):
        first = await self.make_org("A")
        last = await self.make_org("B")
        self.assertEqual((await Organization.objects.afirst()).pk, first.pk)
        self.assertEqual((await Organization.objects.alast()).pk, last.pk)
        self.assertIsNone(await Organization.objects.filter(name="Z").afirst())
        self.assertEqual((await Organization.objects.aearliest("created_at")).pk, first.pk)
        self.assertEqual((await Organization.objects.alatest("created_at")).pk, last.pk)
        bulk = await Organization.objects.ain_bulk([first.pk, last.pk])
        self.assertEqual(set(bulk), {first.pk, last.pk})
        self.assertTrue(await Organization.objects.acontains(first))
        self.assertFalse(await Organization.objects.filter(name="B").acontains(first))

    async def test_lookups_annotations_aggregates(self):
        acme = await self.make_org("Acme", employees=3)
        await self.make_org("Beta", employees=1)
        orgs = [org async for org in Organization.objects.annotate(
            staff=Count("employees")).filter(staff__gte=2)]
        self.assertEqual([(org.name, org.staff) for org in orgs], [("Acme", 3)])
        total = await Employee.objects.filter(organization=acme).aaggregate(total=Sum("salary"))
        self.assertEqual(total, {"total": decimal.Decimal("3003.00")})
        names = [email async for email in Employee.objects.filter(
            Q(email__startswith="acme") & ~Q(email__endswith="0@example.com")
        ).order_by("email").values_list("email", flat=True)]
        self.assertEqual(names, ["acme1@example.com", "acme2@example.com"])
        rows = [row async for row in Organization.objects.values("name").order_by("name")]
        self.assertEqual(rows, [{"name": "Acme"}, {"name": "Beta"}])
        named = [row async for row in Organization.objects.values_list("name", named=True)]
        self.assertEqual(named[0].name, "Acme")
        explain = await Organization.objects.filter(name="Acme").aexplain()
        self.assertIn("Scan", explain)

    async def test_json_decimal_and_timezones(self):
        org = await self.make_org()
        hired = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
        await Employee.objects.acreate(
            organization=org, email="x@example.com", salary=decimal.Decimal("12.50"),
            data={"skills": ["sql"], "level": 3}, hired_at=hired,
        )
        employee = await Employee.objects.aget(data__level=3)
        self.assertEqual(employee.data, {"skills": ["sql"], "level": 3})
        self.assertEqual(employee.salary, decimal.Decimal("12.50"))
        self.assertEqual(employee.hired_at, hired)
        self.assertTrue(await Employee.objects.filter(data__skills__contains=["sql"]).aexists())

    async def test_update_delete_and_cascade(self):
        org = await self.make_org("Acme", employees=2)
        updated = await Employee.objects.filter(organization=org).aupdate(salary=F("salary") + 1)
        self.assertEqual(updated, 2)
        self.assertEqual(
            sorted([e.salary async for e in Employee.objects.all()]),
            [decimal.Decimal("1001.00"), decimal.Decimal("1002.00")],
        )
        deleted, per_model = await Organization.objects.filter(pk=org.pk).adelete()
        self.assertEqual(deleted, 3)
        self.assertEqual(per_model["testapp.Employee"], 2)
        self.assertEqual(await Employee.objects.acount(), 0)

    async def test_bulk_create_bulk_update_get_or_create_update_or_create(self):
        orgs = await Organization.objects.abulk_create(
            [Organization(name=f"O{number}") for number in range(5)]
        )
        self.assertTrue(all(org.pk for org in orgs))
        for org in orgs:
            org.name = org.name.lower()
        self.assertEqual(await Organization.objects.abulk_update(orgs, ["name"]), 5)
        org, created = await Organization.objects.aget_or_create(name="o1")
        self.assertFalse(created)
        org, created = await Organization.objects.aget_or_create(name="new")
        self.assertTrue(created)
        org, created = await Organization.objects.aupdate_or_create(
            name="new", defaults={"name": "renamed"})
        self.assertFalse(created)
        self.assertEqual((await Organization.objects.aget(pk=org.pk)).name, "renamed")

    async def test_integrity_error_is_djangos(self):
        await self.make_org("Acme")
        with self.assertRaises(IntegrityError):
            await Organization.objects.acreate(name="Acme")
        # The connection is still usable afterwards.
        self.assertEqual(await Organization.objects.acount(), 1)


class RelationTests(OffloadGuardedTestCase):
    async def test_select_related_and_lazy_relation_guard(self):
        org = await self.make_org("Acme", employees=1)
        employee = await Employee.objects.select_related("organization").aget()
        self.assertEqual(employee.organization.name, "Acme")
        plain = await Employee.objects.aget()
        # Implicit lazy loads stay forbidden in async code, as in Django.
        with self.assertRaises(SynchronousOnlyOperation):
            plain.organization
        self.assertEqual(org.pk, plain.organization_id)

    async def test_prefetch_related(self):
        acme = await self.make_org("Acme", employees=2)
        tag = await Tag.objects.acreate(name="python")
        async for employee in Employee.objects.all():
            await employee.tags.aadd(tag)
        orgs = [org async for org in Organization.objects.prefetch_related(
            Prefetch("employees", queryset=Employee.objects.order_by("email")),
            "employees__tags",
        )]
        self.assertEqual(len(orgs), 1)
        employees = list(orgs[0].employees.all())
        self.assertEqual([e.email for e in employees], ["acme0@example.com", "acme1@example.com"])
        self.assertEqual([t.name for t in employees[0].tags.all()], ["python"])
        self.assertEqual(acme.pk, orgs[0].pk)

    async def test_related_managers(self):
        org = await self.make_org("Acme")
        employee = await org.employees.acreate(email="a@example.com")
        self.assertEqual(employee.organization_id, org.pk)
        self.assertEqual(await org.employees.acount(), 1)
        boss = await org.employees.acreate(email="boss@example.com")
        await boss.reports.aadd(employee)
        self.assertEqual([e.pk async for e in boss.reports.all()], [employee.pk])
        await boss.reports.aremove(employee)
        self.assertEqual(await boss.reports.acount(), 0)
        python, sql = await Tag.objects.acreate(name="python"), await Tag.objects.acreate(name="sql")
        await employee.tags.aset([python, sql])
        self.assertEqual(await employee.tags.acount(), 2)
        await employee.tags.aremove(python)
        self.assertEqual([t.name async for t in employee.tags.all()], ["sql"])
        go, created = await employee.tags.aget_or_create(name="go")
        self.assertTrue(created)
        await employee.tags.aclear()
        self.assertEqual(await python.employees.acount(), 0)

    async def test_generic_relations_and_raw_querysets(self):
        from asyncdjango.asgi import warm_content_types

        org = await self.make_org("Acme", employees=1)
        employee = await Employee.objects.aget()
        # Generic relation managers look up ContentType synchronously on a
        # cache miss; NativeASGIHandler warms the cache at lifespan startup.
        await warm_content_types()
        await employee.notes.acreate(text="first")
        note = Note(text="second")
        await employee.notes.aadd(note, bulk=False)
        self.assertEqual(sorted([n.text async for n in employee.notes.all()]), ["first", "second"])
        await employee.notes.aclear()
        self.assertEqual(await Note.objects.acount(), 0)
        raw = [o async for o in Organization.objects.raw(
            "SELECT * FROM testapp_organization WHERE name = %s", ["Acme"])]
        self.assertEqual([o.pk for o in raw], [org.pk])

    async def test_model_save_delete_refresh_and_signals(self):
        org = Organization(name="Acme")
        await org.asave()
        self.assertIsNotNone(org.pk)
        employee = Employee(organization=org, email="e@example.com")
        await employee.asave()
        # The synchronous post_save receiver ran its query natively.
        self.assertEqual([log.message async for log in AuditLog.objects.all()],
                         ["hired e@example.com"])
        await Employee.objects.filter(pk=employee.pk).aupdate(salary=99)
        await employee.arefresh_from_db()
        self.assertEqual(employee.salary, 99)
        employee.active = False
        await employee.asave(update_fields=["active"])
        self.assertFalse((await Employee.objects.aget(pk=employee.pk)).active)
        await Profile.objects.acreate(employee=employee, bio="x")
        deleted, _ = await employee.adelete()
        self.assertEqual(deleted, 2)


class StreamingTests(OffloadGuardedTestCase):
    async def test_aiterator_uses_server_side_cursor_in_chunks(self):
        org = await self.make_org()
        await Employee.objects.abulk_create(
            Employee(organization=org, email=f"{number}@example.com")
            for number in range(2500)
        )
        seen = 0
        async for employee in Employee.objects.order_by("id").aiterator(chunk_size=1000):
            seen += 1
        self.assertEqual(seen, 2500)
        emails = [e async for e in Employee.objects.values_list("email", flat=True)
                  .order_by("id").aiterator(chunk_size=700)]
        self.assertEqual(len(emails), 2500)
        # All connections went back to the pool.
        await self.assert_pool_idle()

    async def test_abandoned_aiterator_releases_connection(self):
        org = await self.make_org()
        await Employee.objects.abulk_create(
            Employee(organization=org, email=f"{number}@example.com")
            for number in range(50)
        )
        iterator = Employee.objects.aiterator(chunk_size=10)
        async for _ in iterator:
            break
        await iterator.aclose()
        await self.assert_pool_idle()

    async def test_aiterator_with_prefetch(self):
        org = await self.make_org("Acme", employees=3)
        orgs = [o async for o in Organization.objects.prefetch_related("employees")
                .aiterator(chunk_size=10)]
        self.assertEqual(len(orgs[0].employees.all()), 3)
        self.assertEqual(org.pk, orgs[0].pk)


class ConcurrencyTests(OffloadGuardedTestCase):
    async def test_queries_overlap_on_one_thread(self):
        await Organization.objects.acreate(name="warm")
        thread = threading.get_ident()
        threads = set()

        async def sleeper():
            qs = Organization.objects.extra(select={"slept": "pg_sleep(0.3)"})
            threads.add(threading.get_ident())
            return await qs.afirst()

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(*(sleeper() for _ in range(6)))
        elapsed = loop.time() - started
        self.assertLess(elapsed, 1.2)  # serial execution would take 1.8s
        self.assertEqual(threads, {thread})

    async def test_cancelled_query_returns_connection(self):
        # psycopg cancels the server-side query with a thread unless libpq >= 17
        # provides the non-blocking cancel API; that is cancellation-only.
        self.offload.allow = self.offload.allow | {"cancel"}
        await Organization.objects.acreate(name="warm")
        task = asyncio.create_task(
            Organization.objects.extra(select={"slept": "pg_sleep(5)"}).afirst()
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        await self.assert_pool_idle()
        self.assertEqual(await Organization.objects.acount(), 1)

    async def test_sync_orm_in_async_context_still_raises(self):
        with self.assertRaises(SynchronousOnlyOperation):
            Organization.objects.count()

    async def test_sync_to_native_runs_lazy_relations(self):
        await self.make_org("Acme", employees=2)

        def report():
            return sorted(f"{e.organization.name}:{e.email}" for e in Employee.objects.all())

        self.assertEqual(await sync_to_native(report)(),
                         ["Acme:acme0@example.com", "Acme:acme1@example.com"])

    async def test_debug_query_log(self):
        from django.test import override_settings

        def run():
            from django.db import connection

            list(Organization.objects.filter(name="x"))
            return [query["sql"] for query in connection.queries]

        with override_settings(DEBUG=True):
            queries = await sync_to_native(run)()
        self.assertEqual(len(queries), 1)
        self.assertIn("\"name\" = 'x'", queries[0])

    async def test_connection_created_signal_and_execute_wrappers(self):
        from django.db.backends.signals import connection_created

        seen = []

        def install(sender, connection, **kwargs):
            def wrapper(execute, sql, params, many, context):
                seen.append(sql.split()[0])
                return execute(sql, params, many, context)

            connection.execute_wrappers.append(wrapper)

        connection_created.connect(install)
        try:
            await Organization.objects.acount()
        finally:
            connection_created.disconnect(install)
        self.assertEqual(seen, ["SELECT"])
        # Outside any scope, connections[...] is still Django's own wrapper.
        self.assertIsNone(connections["default"].connection)
