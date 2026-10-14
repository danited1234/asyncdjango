"""Model metadata and SQL compilation without a database connection."""

import unittest

from asyncdjango.db import models


class User(models.Model):
    email = models.CharField(max_length=255)
    age = models.IntegerField(null=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "users"


class ModelTests(unittest.TestCase):
    def test_model_fields_defaults_and_primary_key(self) -> None:
        user = User(email="a@example.com")
        self.assertIsNone(user.pk)
        self.assertTrue(user.active)
        user.pk = 3
        self.assertEqual(user.id, 3)
        self.assertEqual(User._meta.pk_name, "id")
        self.assertEqual(User._meta.db_table, "users")
        with self.assertRaisesRegex(TypeError, "Unknown model fields"):
            User(no_such_field=1)

    def test_query_is_lazy_and_parameterized(self) -> None:
        query = (
            User.objects.filter(models.Q(age__gte=18) | models.Q(age__isnull=True))
            .filter(email="x' OR 1=1 --")
            .exclude(active=False)
            .order_by("-age", "email")
        )
        sql, params = query.compile()
        self.assertEqual(
            sql,
            'SELECT "id", "email", "age", "active" FROM "users" '
            'WHERE (("age" >= $1) OR ("age" IS NULL)) '
            'AND ("email" = $2) AND NOT (("active" = $3)) '
            'ORDER BY "age" DESC, "email" ASC',
        )
        self.assertEqual(params, (18, "x' OR 1=1 --", False))
        self.assertNotIn("OR 1=1", sql)

    def test_queryset_clones_and_empty_in(self) -> None:
        base = User.objects.filter(active=True)
        other = base.filter(id__in=[])
        self.assertNotEqual(base.compile(), other.compile())
        self.assertIn("FALSE", other.compile()[0])
        self.assertEqual(base.compile()[1], (True,))

    def test_rejects_unknown_fields_and_lookups(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown ordering field"):
            User.objects.order_by("unexpected")
        with self.assertRaisesRegex(ValueError, "Unknown model field"):
            User.objects.filter(unexpected=1).compile()
        with self.assertRaisesRegex(ValueError, "Unsupported lookup"):
            User.objects.filter(age__contains=1).compile()
        with self.assertRaisesRegex(TypeError, "isnull requires a bool"):
            User.objects.filter(age__isnull="yes").compile()

    def test_explicit_primary_key_and_duplicate_column_check(self) -> None:
        class Article(models.Model):
            slug = models.CharField(max_length=80, primary_key=True)

        self.assertEqual(list(Article._meta.fields), ["slug"])
        with self.assertRaisesRegex(TypeError, "distinct database columns"):
            class BadModel(models.Model):
                first = models.IntegerField(db_column="duplicate")
                second = models.IntegerField(db_column="duplicate")

    def test_foreign_key_column_and_relation_contract(self) -> None:
        class Organization(models.Model):
            name = models.CharField(max_length=50)

        class Employee(models.Model):
            organization = models.ForeignKey(Organization, on_delete=models.CASCADE)

        employee = Employee(organization_id=3)
        self.assertEqual(employee.organization_id, 3)
        with self.assertRaises(models.UnloadedRelationError):
            _ = employee.organization
        organization = Organization(id=3, name="Acme")
        employee.organization = organization
        self.assertIs(employee.organization, organization)
        sql, params = Employee.objects.filter(organization_id=3).compile()
        self.assertIn('"organization_id" = $1', sql)
        self.assertEqual(params, (3,))
        with self.assertRaisesRegex(ValueError, "Not a foreign key"):
            Employee.objects.select_related("id")

    def test_multilevel_select_related_joins_each_prefix_once(self) -> None:
        class Company(models.Model):
            name = models.CharField(max_length=30)

        class Department(models.Model):
            company = models.ForeignKey(Company, on_delete=models.CASCADE)

        class Employee(models.Model):
            department = models.ForeignKey(Department, on_delete=models.CASCADE)

        sql, _ = (Employee.objects
                  .select_related("department__company", "department")
                  .compile())
        self.assertEqual(sql.count('LEFT JOIN "test_models_department"'), 1)
        self.assertEqual(sql.count('LEFT JOIN "test_models_company"'), 1)
        self.assertIn('"t1"."company_id" = "t2"."id"', sql)
        with self.assertRaisesRegex(ValueError, "Not a foreign key"):
            Employee.objects.select_related("department__missing")

    def test_reverse_foreign_key_is_lazy_queryset(self) -> None:
        class Organization(models.Model):
            name = models.CharField(max_length=30)

        class Employee(models.Model):
            organization = models.ForeignKey(
                Organization, on_delete=models.CASCADE, related_name="staff"
            )

        parent = Organization(id=7, name="Acme")
        sql, params = parent.staff.order_by("id").compile()
        self.assertIn('"organization_id" = $1', sql)
        self.assertEqual(params, (7,))
        self.assertIs(parent.staff.model, Employee)
        with self.assertRaisesRegex(ValueError, "Save the parent"):
            _ = Organization(name="Unsaved").staff

    def test_many_to_many_descriptor_does_not_evaluate_implicitly(self) -> None:
        class Tag(models.Model):
            name = models.CharField(max_length=20)

        class Article(models.Model):
            tags = models.ManyToManyField(Tag, related_name="articles")

        article = Article(id=1)
        self.assertIs(article.tags.model, Tag)
        self.assertIs(Tag(id=2, name="tag").articles.model, Article)
        self.assertIn("tags", Article._meta.many_to_many)
        self.assertEqual(Article.tags.through_table, "test_models_article_tags")
        with self.assertRaises(AttributeError):
            article.tags = []
