"""asyncdjango.rest_framework through NativeASGIHandler, with no thread offload."""

from asyncdjango.asgi import NativeASGIHandler
from asyncdjango.test import ASGIClient

from .test_orm import OffloadGuardedTestCase
from .testapp.api import EmployeeSerializer, OrganizationSerializer
from .testapp.models import AuditLog, Employee, Organization, Tag


class RestTests(OffloadGuardedTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = ASGIClient(NativeASGIHandler())

    async def test_viewset_list_pagination_ordering_and_crud(self):
        for name in ["c", "a", "b"]:
            await Organization.objects.acreate(name=name)
        response = await self.client.get("/api/organizations/", query_string="ordering=-name")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual([o["name"] for o in body["results"]], ["c", "b"])

        created = await self.client.post("/api/organizations/", {"name": "d"})
        self.assertEqual(created.status_code, 201, created.content)
        pk = created.json()["id"]
        duplicate = await self.client.post("/api/organizations/", {"name": "d"})
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("name", duplicate.json())

        detail = await self.client.get(f"/api/organizations/{pk}/")
        self.assertEqual(detail.json(), {"id": pk, "name": "d"})
        patched = await self.client.patch(f"/api/organizations/{pk}/", {"name": "e"})
        self.assertEqual(patched.json()["name"], "e")
        missing = await self.client.get("/api/organizations/999999/")
        self.assertEqual(missing.status_code, 404)
        deleted = await self.client.delete(f"/api/organizations/{pk}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(await Organization.objects.acount(), 3)

    async def test_generic_views_with_lazy_relations_and_async_method_fields(self):
        org = await Organization.objects.acreate(name="Acme")
        tag = await Tag.objects.acreate(name="python")
        response = await self.client.post("/api/employees/", {
            "email": "boss@example.com", "organization": org.pk, "tags": [tag.pk],
        })
        self.assertEqual(response.status_code, 201, response.content)
        boss = response.json()
        self.assertEqual(boss["organization_name"], "Acme")
        self.assertEqual(boss["tags"], [tag.pk])
        report = await Employee.objects.acreate(email="r@example.com", organization=org)
        await Employee.objects.filter(pk=report.pk).aupdate(manager_id=boss["id"])

        listing = await self.client.get("/api/employees/")
        self.assertEqual([(e["email"], e["report_count"]) for e in listing.json()],
                         [("boss@example.com", 1), ("r@example.com", 0)])
        bad = await self.client.put(f"/api/employees/{boss['id']}/", {"email": "not-an-email"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(set(bad.json()), {"email", "organization"})
        removed = await self.client.delete(f"/api/employees/{report.pk}/")
        self.assertEqual(removed.status_code, 204)

    async def test_crm_style_auth_async_permission_and_sync_throttle(self):
        await Organization.objects.acreate(name="Acme")
        anonymous = await self.client.get("/api/me/")
        self.assertEqual(anonymous.status_code, 401)
        unknown = await self.client.get("/api/me/", headers={"Authorization": "Bearer Nope"})
        self.assertEqual(unknown.status_code, 401)
        me = await self.client.get("/api/me/", headers={"Authorization": "Bearer Acme"})
        self.assertEqual(me.status_code, 200, me.content)
        self.assertEqual(me.json()["organization"]["name"], "Acme")
        throttled = await self.client.get(
            "/api/me/", headers={"Authorization": "Bearer Acme", "X-Throttle": "deny"})
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(throttled.headers["retry-after"], "7")
        self.assertEqual(await AuditLog.objects.acount(), 2)

    async def test_api_view_function(self):
        await Organization.objects.acreate(name="Acme")
        response = await self.client.get("/api/summary/")
        self.assertEqual(response.json(), {"organizations": 1})

    async def test_serializer_async_api(self):
        org = await Organization.objects.acreate(name="Acme")
        serializer = OrganizationSerializer(data={"name": "Acme"})
        self.assertFalse(await serializer.ais_valid())
        serializer = OrganizationSerializer(data={"name": "Beta"})
        self.assertTrue(await serializer.ais_valid())
        beta = await serializer.asave()
        self.assertEqual((await serializer.adata)["name"], "Beta")
        many = await OrganizationSerializer(Organization.objects.order_by("name"), many=True).adata
        self.assertEqual([o["name"] for o in many], ["Acme", "Beta"])
        employee = await Employee.objects.acreate(email="e@example.com", organization=org)
        data = await EmployeeSerializer(employee).adata
        self.assertEqual((data["organization_name"], data["report_count"]), ("Acme", 0))
        self.assertNotEqual(beta.pk, org.pk)
