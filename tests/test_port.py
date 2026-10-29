"""python -m asyncdjango.port: the ADRF porting helper."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from asyncdjango.port import main

SOURCE = '''\
from adrf.views import APIView
from adrf import serializers
import adrf.generics
from adrf.test import AsyncAPIClient
from asgiref.sync import sync_to_async


def build(org):
    return list(org.members.all())


class View(APIView):
    async def post(self, request):
        s = Ser(data=request.data)
        await sync_to_async(s.is_valid)(raise_exception=True)
        payload = await sync_to_async(lambda: s.data)()
        await sync_to_async(build)(self.org)
        await sync_to_async(stripe.Charge.create)(amount=1)
        await sync_to_async(lambda: generate_invoice_pdf(self.org))()
        await sync_to_async(lambda: user.email)()
        await sync_to_async(helper_elsewhere)()
        return "café"


@sync_to_async
def save_it(obj):
    obj.save()
'''


class PortTests(unittest.TestCase):
    def run_port(self, *args):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "views.py"
            path.write_bytes(SOURCE.replace("\n", "\r\n").encode())
            (Path(directory) / ".venv").mkdir()
            (Path(directory) / ".venv" / "skipped.py").write_text("from adrf.views import X\n")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main([directory, *args])
            return output.getvalue(), path.read_bytes().decode()

    def test_dry_run_reports_without_writing(self):
        report, after = self.run_port()
        self.assertEqual(after, SOURCE.replace("\n", "\r\n"))
        self.assertIn("To rewrite (run with --write) (4):", report)
        self.assertNotIn("skipped.py", report)
        for header in ("adrf modules without an asyncdjango equivalent (1):",
                       "Plain `import adrf...`: change the import and its uses by hand (1):",
                       "Database only: candidates for sync_to_native (2):",
                       "Blocking I/O: keep sync_to_async (2):",
                       # helper_elsewhere, and user.email (a lazy relation? cannot tell)
                       "Unknown: read the wrapped code (2):"):
            self.assertIn(f"\n{header}\n", report)
        self.assertIn("[stripe]", report)
        self.assertIn("[generate_invoice_pdf]", report)

    def test_write_rewrites_imports_and_serializer_calls(self):
        _, after = self.run_port("--write")
        self.assertIn("from asyncdjango.rest_framework.views import APIView\r\n", after)
        self.assertIn("from asyncdjango.rest_framework import serializers\r\n", after)
        self.assertIn("        await s.ais_valid(raise_exception=True)\r\n", after)
        self.assertIn("        payload = await s.adata\r\n", after)
        self.assertIn("import adrf.generics\r\n", after)  # left for a person
        self.assertIn('return "café"', after)
        compile(after, "views.py", "exec")
