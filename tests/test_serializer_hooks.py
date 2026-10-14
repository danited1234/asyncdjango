"""Custom serializer hooks and explicit writable nested relation behavior."""

import importlib.util
import os
import unittest

from asyncdjango.db import models, using_database
from asyncdjango.serializers import ModelSerializer, ValidationError


class Organization(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        db_table = "asyncdjango_serializer_organizations"


class Employee(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    code = models.CharField(max_length=30, unique=True)

    class Meta:
        db_table = "asyncdjango_serializer_employees"


class OrganizationSerializer(ModelSerializer):
    class Meta:
        model = Organization
        fields = ("id", "name")

    def validate_name(self, value):
        if not value.strip():
            raise ValidationError("Name cannot be blank")
        return value.strip()


class EmployeeSerializer(ModelSerializer):
    organization = OrganizationSerializer()

    class Meta:
        model = Employee
        fields = ("id", "organization", "code")

    async def validate_code(self, value):
        if not value.upper().startswith("EMP-"):
            raise ValidationError("Code must start with EMP-")
        return value.upper()

    def validate(self, attrs):
        if attrs.get("code") == "EMP-BLOCKED":
            raise ValidationError({"code": "Blocked code"})
        return attrs

    async def create(self, validated_data):
        org_data = validated_data.pop("organization")
        org = await Organization.objects.create(**org_data)
        employee = await Employee.objects.create(organization=org, **validated_data)
        employee.organization = org
        return employee

    async def update(self, instance, validated_data):
        org_data = validated_data.pop("organization", None)
        if org_data is not None:
            org = await Organization.objects.get(pk=instance.organization_id)
            for name, value in org_data.items():
                setattr(org, name, value)
            await org.save()
            instance.organization = org
        for name, value in validated_data.items():
            setattr(instance, name, value)
        await instance.save()
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["label"] = data["code"].lower()
        return data


class DefaultNestedSerializer(ModelSerializer):
    organization = OrganizationSerializer()

    class Meta:
        model = Employee
        fields = ("organization", "code")


class Plain(models.Model):
    value = models.CharField(max_length=20)


class HooksSerializer(ModelSerializer):
    class Meta:
        model = Plain
        fields = ("value",)

    def to_internal_value(self, data):
        return super().to_internal_value({"value": data["value"].strip()})

    def validate_value(self, value):
        return value.lower()

    async def validate(self, attrs):
        if attrs["value"] == "forbidden":
            raise ValidationError("Forbidden value")
        return attrs

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result["value"] = result["value"].upper()
        return result


class SerializerHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_object_and_output_hooks(self):
        serializer = HooksSerializer(data={"value": "  HELLO "})
        self.assertTrue(await serializer.ais_valid())
        self.assertEqual(serializer.validated_data, {"value": "hello"})
        self.assertEqual(HooksSerializer(Plain(value="hello")).data,
                         {"value": "HELLO"})
        invalid = HooksSerializer(data={"value": "forbidden"})
        self.assertFalse(await invalid.ais_valid())
        self.assertIn("non_field_errors", invalid.errors)

    async def test_nested_validation_and_custom_hooks(self):
        bad = EmployeeSerializer(data={"organization": {"name": " "},
                                       "code": "bad"})
        self.assertFalse(bad.is_valid())
        self.assertIn("name", bad.errors["organization"])
        serializer = EmployeeSerializer(data={"organization": {"name": "  Acme  "},
                                              "code": "EMP-A"})
        self.assertTrue(serializer.is_valid())
        self.assertEqual(serializer.validated_data["organization"]["name"], "Acme")
        blocked = EmployeeSerializer(data={"organization": {"name": "Acme"},
                                           "code": "EMP-BLOCKED"})
        self.assertFalse(blocked.is_valid())
        self.assertIn("code", blocked.errors)


HAS_DATABASE = bool(os.environ.get("ASYNCDJANGO_DATABASE_URL"))
HAS_DRIVER = importlib.util.find_spec("asyncpg") is not None


@unittest.skipUnless(HAS_DATABASE and HAS_DRIVER, "PostgreSQL URL and asyncpg required")
class NestedWriteIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from asyncdjango.db import AsyncPostgres
        from asyncdjango.db.migrations import apply_migration, initial_migration

        self.database = AsyncPostgres(os.environ["ASYNCDJANGO_DATABASE_URL"])
        await self.database.connect()
        self.migration = initial_migration("test_0004_nested_serializer",
                                           [Organization, Employee])
        await apply_migration(self.database, self.migration)

    async def asyncTearDown(self):
        await self.database.execute('DROP TABLE IF EXISTS "asyncdjango_serializer_employees"')
        await self.database.execute('DROP TABLE IF EXISTS "asyncdjango_serializer_organizations"')
        await self.database.execute(
            'DELETE FROM "_asyncdjango_migrations" WHERE "name" = $1',
            self.migration.name,
        )
        await self.database.close()

    async def test_explicit_nested_create_update_and_atomic_rollback(self):
        with using_database(self.database):
            invalid_code = EmployeeSerializer(data={
                "organization": {"name": "Acme"}, "code": "BAD"
            })
            self.assertFalse(await invalid_code.ais_valid())
            self.assertIn("code", invalid_code.errors)
            serializer = EmployeeSerializer(data={
                "organization": {"name": "Acme"}, "code": "EMP-ONE"
            })
            self.assertTrue(await serializer.ais_valid())
            created = await serializer.asave()
            self.assertEqual(serializer.data["organization"]["name"], "Acme")
            self.assertEqual(serializer.data["label"], "emp-one")

            update = EmployeeSerializer(created, data={
                "organization": {"name": "New Acme"}, "code": "EMP-TWO"
            })
            self.assertTrue(await update.ais_valid())
            await update.asave()
            self.assertEqual((await Organization.objects.get(
                pk=created.organization_id
            )).name, "New Acme")

            duplicate = EmployeeSerializer(data={
                "organization": {"name": "Should Roll Back"}, "code": "emp-two"
            })
            self.assertFalse(await duplicate.ais_valid())
            self.assertEqual(await Organization.objects.count(), 1)

            unsupported = DefaultNestedSerializer(data={
                "organization": {"name": "Unsupported"}, "code": "EMP-THREE"
            })
            self.assertTrue(await unsupported.ais_valid())
            with self.assertRaises(ValidationError):
                await unsupported.asave()
            self.assertEqual(await Organization.objects.count(), 1)

    async def test_nested_write_failure_rolls_back_related_row(self):
        class FailingEmployeeSerializer(EmployeeSerializer):
            async def create(self, validated_data):
                await super().create(validated_data)
                raise ValidationError({"code": "Fail after both inserts"})

        with using_database(self.database):
            serializer = FailingEmployeeSerializer(data={
                "organization": {"name": "Rollback"}, "code": "EMP-ROLLBACK"
            })
            self.assertTrue(await serializer.ais_valid())
            with self.assertRaises(ValidationError):
                await serializer.asave()
            self.assertEqual(await Organization.objects.count(), 0)
            self.assertEqual(await Employee.objects.count(), 0)
