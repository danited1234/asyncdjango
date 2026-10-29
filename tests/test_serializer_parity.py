"""asyncdjango.rest_framework serializers return exactly what DRF's return."""

import decimal

from rest_framework import serializers as drf

from asyncdjango import sync_to_native
from asyncdjango.rest_framework import serializers as ours

from .test_orm import OffloadGuardedTestCase
from .testapp.models import Employee, Organization, Profile, Tag


def employee_serializer(base):
    class OrganizationSerializer(base.ModelSerializer):
        class Meta:
            model = Organization
            fields = ["id", "name"]

    class EmployeeSerializer(base.ModelSerializer):
        organization = OrganizationSerializer(read_only=True)
        organization_name = base.CharField(source="organization.name")
        organization_str = base.CharField(source="organization.__str__")  # a method
        deferred = base.CharField(source="get_deferred_fields")  # a one-name method
        profile_bio = base.CharField(source="profile.bio", default="none")
        profile_obj = base.CharField(source="profile", default="none")  # missing one-to-one
        missing = base.CharField(source="no_such_attribute", required=False)
        missing_default = base.CharField(source="no_such_attribute", default="fallback")
        doubled = base.SerializerMethodField()
        password = base.CharField(write_only=True, default="secret")
        everything = base.SerializerMethodField(method_name="get_whole")

        class Meta:
            model = Employee
            fields = ["id", "email", "salary", "active", "data", "hired_at", "manager", "tags",
                      "organization", "organization_name", "organization_str", "deferred",
                      "profile_bio", "profile_obj", "missing", "missing_default", "doubled",
                      "password",
                      "everything"]

        def get_doubled(self, obj):
            return obj.salary * 2

        def get_whole(self, obj):
            return obj.email if hasattr(obj, "email") else obj["email"]

    return EmployeeSerializer


class SerializerParityTests(OffloadGuardedTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        org = await Organization.objects.acreate(name="Acme")
        boss = await Employee.objects.acreate(organization=org, email="boss@example.com",
                                              salary=decimal.Decimal("10.50"))
        worker = await Employee.objects.acreate(organization=org, email="w@example.com",
                                                manager=boss, data={"k": [1, 2]})
        await Profile.objects.acreate(employee=worker, bio="hello")
        tag = await Tag.objects.acreate(name="t")
        await worker.tags.aadd(tag)

    async def test_same_output_as_drf(self):
        ours_class, drf_class = employee_serializer(ours), employee_serializer(drf)

        def both(instance, **kwargs):
            return ours_class(instance, **kwargs).data, drf_class(instance, **kwargs).data

        def compare():
            employees = list(Employee.objects.order_by("id"))
            for employee in employees:
                mine, theirs = both(employee)
                self.assertEqual(mine, theirs)
            mine, theirs = both(employees, many=True)
            self.assertEqual(mine, theirs)
            # A second representation through the same serializer reuses its plan.
            serializer = ours_class(employees, many=True)
            self.assertEqual(serializer.to_representation(employees), theirs)
            self.assertEqual(serializer.to_representation(employees), theirs)
            return theirs

        data = await sync_to_native(compare)()
        self.assertEqual(data[1]["profile_bio"], "hello")
        self.assertEqual(data[0]["profile_bio"], None)
        self.assertNotIn("missing", data[0])
        self.assertEqual(data[0]["missing_default"], "fallback")
        self.assertNotIn("password", data[0])
        self.assertEqual(data[0]["deferred"], "set()")

    async def test_same_output_for_mappings_and_async_data(self):
        def mapping_serializer(base):
            class RowSerializer(base.Serializer):
                email = base.EmailField()
                salary = base.DecimalField(max_digits=10, decimal_places=2)
                organization_name = base.CharField(source="organization.name")
                missing = base.CharField(required=False)
                missing_default = base.CharField(default="fallback")

            return RowSerializer

        row = {"email": "m@example.com", "salary": decimal.Decimal("1.5"),
               "organization": {"name": "x"}}
        self.assertEqual(await sync_to_native(lambda: mapping_serializer(ours)(row).data)(),
                         mapping_serializer(drf)(row).data)

        ours_class, drf_class = employee_serializer(ours), employee_serializer(drf)
        employees = [e async for e in Employee.objects.order_by("id")]
        expected = await sync_to_native(lambda: drf_class(employees, many=True).data)()
        self.assertEqual(await ours_class(employees, many=True).adata, expected)

    async def test_fields_are_copied_per_instance(self):
        serializer_class = employee_serializer(ours)
        first, second = serializer_class(), serializer_class()
        self.assertIsNot(first.fields["email"], second.fields["email"])
        self.assertIs(first.fields["email"].parent, first)
        first.fields.pop("email")
        self.assertIn("email", serializer_class().fields)

    async def test_classes_that_customize_field_building_keep_drfs_path(self):
        class Custom(ours.ModelSerializer):
            class Meta:
                model = Organization
                fields = ["id", "name"]

            def get_field_names(self, declared_fields, info):
                return ["name"] if self.context.get("short") else ["id", "name"]

        self.assertEqual(list(Custom(context={"short": True}).fields), ["name"])
        self.assertEqual(list(Custom().fields), ["id", "name"])
