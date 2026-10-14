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
