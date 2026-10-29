"""ASYNCDJANGO["SESSION_SQL"]: per-request session state on pooled connections."""

import contextvars
from unittest import mock

from django.db import connection, transaction
from django.test import override_settings

from asyncdjango import sync_to_native
from asyncdjango.db import session

from .test_orm import OffloadGuardedTestCase
from .testapp.models import Organization

tenant = contextvars.ContextVar("tenant", default="public")


def session_sql(connection):
    return f"SET application_name TO '{tenant.get()}'"


@sync_to_native
def application_name():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('application_name')")
        return cursor.fetchone()[0]


class SessionSQLTests(OffloadGuardedTestCase):
    async def asyncSetUp(self):
        # One pooled connection, so every query below shares its session state.
        self.settings = override_settings(ASYNCDJANGO={
            "POOL": {"min_size": 1, "max_size": 1},
            "SESSION_SQL": "tests.test_session_sql.session_sql",
        })
        self.settings.enable()
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.settings.disable()

    async def test_each_query_sees_the_current_value(self):
        tenant.set("acme")
        self.assertEqual(await application_name(), "acme")
        tenant.set("globex")
        self.assertEqual(await application_name(), "globex")

    async def test_runs_only_when_the_connection_state_differs(self):
        tenant.set("acme")
        with mock.patch.object(session, "_run", wraps=session._run) as run:
            for _ in range(3):
                await Organization.objects.acount()
            async with transaction.atomic():
                tenant.set("globex")
                for _ in range(3):
                    await Organization.objects.acount()
            self.assertEqual(await application_name(), "globex")
        self.assertEqual(run.call_count, 2)

    async def test_rolled_back_set_is_applied_again(self):
        tenant.set("acme")
        self.assertEqual(await application_name(), "acme")
        tenant.set("globex")
        with self.assertRaises(ZeroDivisionError):
            async with transaction.atomic():
                self.assertEqual(await application_name(), "globex")
                1 / 0
        # The rollback restored "acme" on the (only) pooled connection.
        self.assertEqual(await application_name(), "globex")

    async def test_rollback_to_savepoint(self):
        tenant.set("acme")
        async with transaction.atomic():
            await application_name()
            with self.assertRaises(ZeroDivisionError):
                async with transaction.atomic():
                    tenant.set("globex")  # SET inside the savepoint
                    self.assertEqual(await application_name(), "globex")
                    1 / 0
            tenant.set("globex")
            self.assertEqual(await application_name(), "globex")
            tenant.set("acme")
            self.assertEqual(await application_name(), "acme")

    async def test_set_inside_a_rolled_back_savepoint_is_forgotten(self):
        tenant.set("acme")
        async with transaction.atomic():
            await application_name()
            with self.assertRaises(ZeroDivisionError):
                async with transaction.atomic():
                    tenant.set("globex")
                    await application_name()
                    1 / 0
            # The session is back to "acme"; asking for "globex" must SET it again.
            tenant.set("globex")
            self.assertEqual(await application_name(), "globex")
