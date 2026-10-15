"""Guards: no thread offload in the source, every Django async method patched."""

import ast
import asyncio
import inspect
import time
import unittest
from pathlib import Path

from django.contrib.contenttypes.fields import create_generic_related_manager
from django.core import checks
from django.db.models import Model, QuerySet
from django.db.models.query import RawQuerySet
from django.test import SimpleTestCase, override_settings

from asyncdjango.db.patch import is_native
from asyncdjango.diagnostics import EventLoopBlockDetector, ThreadOffloadDetector
from asyncdjango.test import NativeTransactionTestCase

from .testapp.models import Employee, Organization

SOURCE = Path(__file__).resolve().parents[1] / "src" / "asyncdjango"
FORBIDDEN = {"sync_to_async", "to_thread", "run_in_executor", "ThreadPoolExecutor",
             "database_sync_to_async"}
# diagnostics.ThreadOffloadDetector intercepts run_in_executor (test tooling);
# _threads.py holds the documented, deliberate thread uses.
ALLOWED_FILES = {"diagnostics.py", "_threads.py"}


class SourceGuardTests(unittest.TestCase):
    def test_source_has_no_thread_offload(self):
        violations = []
        for path in SOURCE.rglob("*.py"):
            if path.name in ALLOWED_FILES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                name = (node.id if isinstance(node, ast.Name)
                        else node.attr if isinstance(node, ast.Attribute)
                        else None)
                if name in FORBIDDEN:
                    violations.append(f"{path.relative_to(SOURCE)}:{node.lineno}:{name}")
                if isinstance(node, ast.ImportFrom) and node.module == "asgiref.sync":
                    violations.extend(
                        f"{path.relative_to(SOURCE)}:{node.lineno}:{alias.name}"
                        for alias in node.names if alias.name in FORBIDDEN
                    )
        self.assertEqual(violations, [])


def offloading_methods(cls):
    """Async methods whose Django implementation calls sync_to_async."""
    found = []
    for klass in cls.__mro__:
        if not klass.__module__.startswith("django."):
            continue
        for name, value in vars(klass).items():
            code = getattr(value, "__code__", None)
            if code is not None and code.co_name == "manager_method":
                # Manager proxies call the (patched) QuerySet method at runtime.
                continue
            original = getattr(value, "__wrapped__", value)
            if not (inspect.iscoroutinefunction(original) or inspect.isasyncgenfunction(original)
                    or name == "__aiter__"):
                continue
            try:
                source = inspect.getsource(original)
            except (OSError, TypeError):
                continue
            if "sync_to_async" in source or "_async_generator" in source:
                found.append(name)
    return sorted(set(found))


class CoverageTests(SimpleTestCase):
    def assert_all_native(self, cls):
        missing = [name for name in offloading_methods(cls)
                   if not is_native(getattr(cls, name))]
        self.assertEqual(missing, [], f"{cls.__name__} still offloads to threads")

    def test_queryset_model_and_related_managers_are_native(self):
        self.assert_all_native(QuerySet)
        self.assert_all_native(RawQuerySet)
        self.assert_all_native(Model)
        org = Organization(pk=1)
        employee = Employee(pk=1, organization=org)
        self.assert_all_native(type(org.employees))       # reverse foreign key
        self.assert_all_native(type(employee.tags))       # many-to-many
        self.assert_all_native(type(employee.reports))    # self-referential FK
        from django.contrib.auth.models import Group, Permission

        self.assert_all_native(create_generic_related_manager(Permission._default_manager.__class__, None))
        self.assertTrue(is_native(Group.objects.none().aget))


class DetectorTests(NativeTransactionTestCase):
    async def test_detects_djangos_thread_based_method(self):
        original = vars(QuerySet)["acount"].__wrapped__
        async with ThreadOffloadDetector(forbid=False) as detector:
            await original(Organization.objects.all())
        self.assertTrue(detector.calls)
        # That worker thread now holds its own Django connection; close it there.
        from asgiref.sync import sync_to_async
        from django.db import connections

        await sync_to_async(connections.close_all)()
        async with ThreadOffloadDetector() as detector:
            await Organization.objects.acount()
        self.assertEqual(detector.calls, [])

    async def test_flags_blocking_sleep(self):
        with self.assertRaisesRegex(RuntimeError, "Event loop blocked"):
            async with EventLoopBlockDetector(interval=0.005, threshold=0.02):
                time.sleep(0.06)

    async def test_allows_nonblocking_sleep(self):
        async with EventLoopBlockDetector(interval=0.005, threshold=0.05):
            await asyncio.sleep(0.02)


class CheckTests(SimpleTestCase):
    @override_settings(MIDDLEWARE=["tests.testapp.middleware.SyncCountingMiddleware"])
    def test_sync_middleware_warning(self):
        ids = [message.id for message in checks.run_checks()]
        self.assertIn("asyncdjango.W002", ids)

    @override_settings(
        MIDDLEWARE=["tests.testapp.middleware.SyncCountingMiddleware"],
        ASYNCDJANGO={"NATIVE_SYNC_MIDDLEWARE": ["tests.testapp.middleware.SyncCountingMiddleware"]},
    )
    def test_listed_sync_middleware_is_accepted(self):
        ids = [message.id for message in checks.run_checks()]
        self.assertNotIn("asyncdjango.W002", ids)


SCRIPT_WITHOUT_CLOSE = """
import asyncio, os, sys
os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"
import django
from django.conf import settings
django.setup()
settings.ASYNCDJANGO = {"POOL": {"min_size": 6, "max_size": 8}}
from django.db import connection
from asyncdjango import sync_to_native

def query():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone()[0]

async def main():
    print(await sync_to_native(query)())

loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
asyncio.run(main(), loop_factory=loop_factory)
"""


class ShutdownTests(unittest.TestCase):
    def test_asyncio_run_exits_without_closing_pools(self):
        import os
        import subprocess
        import sys

        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        completed = subprocess.run([sys.executable, "-c", SCRIPT_WITHOUT_CLOSE],
                                   capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "1")
