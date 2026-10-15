"""PATCH_ORM = False: models opt in with NativeManager and NativeModelMixin."""

from asgiref.sync import sync_to_async
from django.db import connections

from asyncdjango.db import patch
from asyncdjango.diagnostics import ThreadOffloadDetector
from asyncdjango.test import NativeTransactionTestCase

from .testapp.models import NativeOrganization, Organization


class OptInTests(NativeTransactionTestCase):
    def setUp(self):
        patch.uninstall()

    def tearDown(self):
        patch.install()
        super().tearDown()

    async def test_only_opted_in_models_are_native(self):
        async with ThreadOffloadDetector() as detector:
            org = NativeOrganization(name="native")
            await org.asave()
            self.assertEqual(await NativeOrganization.objects.acount(), 1)
            self.assertEqual([o.name async for o in NativeOrganization.objects.all()],
                             ["native"])
            await org.adelete()
        self.assertEqual(detector.calls, [])
        async with ThreadOffloadDetector(forbid=False) as detector:
            await Organization.objects.acount()
        self.assertTrue(detector.calls)
        await sync_to_async(connections.close_all)()
