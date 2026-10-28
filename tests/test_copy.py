import psycopg
from django.db import connection, transaction

from asyncdjango import sync_to_native

from .test_orm import OffloadGuardedTestCase
from .testapp.models import Tag


def copy_names_in(names):
    table = Tag._meta.db_table
    with connection.cursor() as cursor:
        with cursor.copy(f"COPY {table} (name) FROM STDIN") as copy:
            for name in names:
                copy.write_row((name,))


def copy_names_out():
    table = Tag._meta.db_table
    with connection.cursor() as cursor:
        with cursor.copy(f"COPY (SELECT name FROM {table} ORDER BY name) TO STDOUT") as copy:
            return [row[0] for row in copy.rows()]


class CopyTests(OffloadGuardedTestCase):
    async def test_copy_from_and_to_through_django_cursor(self):
        await sync_to_native(copy_names_in)(["sql", "python", "go"])
        self.assertEqual(await Tag.objects.acount(), 3)
        self.assertEqual(await sync_to_native(copy_names_out)(), ["go", "python", "sql"])

    async def test_copy_write_raw_data_and_read_blocks(self):
        def run():
            table = Tag._meta.db_table
            with connection.cursor() as cursor:
                with cursor.copy(f"COPY {table} (name) FROM STDIN") as copy:
                    copy.write(b"alpha\nbeta\n")
                query = f"COPY (SELECT name FROM {table} ORDER BY name) TO STDOUT"
                with cursor.copy(query) as copy:
                    return b"".join(bytes(block) for block in copy)

        self.assertEqual(await sync_to_native(run)(), b"alpha\nbeta\n")

    async def test_copy_joins_the_open_transaction(self):
        try:
            async with transaction.atomic():
                await sync_to_native(copy_names_in)(["rolled-back"])
                self.assertEqual(await Tag.objects.acount(), 1)
                raise RuntimeError
        except RuntimeError:
            pass
        self.assertEqual(await Tag.objects.acount(), 0)

    async def test_copy_error_leaves_connection_usable(self):
        await Tag.objects.acreate(name="dup")
        # As with Django's own psycopg cursor, COPY errors are not wrapped.
        with self.assertRaises(psycopg.errors.UniqueViolation):
            await sync_to_native(copy_names_in)(["dup"])
        self.assertEqual(await Tag.objects.acount(), 1)
        await self.assert_pool_idle()
