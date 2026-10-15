from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


class Organization(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)


class Note(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("content_type", "object_id")
    text = models.CharField(max_length=100)


class Employee(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="employees"
    )
    manager = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reports"
    )
    email = models.EmailField(unique=True)
    salary = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    active = models.BooleanField(default=True)
    data = models.JSONField(default=dict)
    tags = models.ManyToManyField(Tag, related_name="employees", blank=True)
    hired_at = models.DateTimeField(null=True, blank=True)
    notes = GenericRelation(Note)

    class Meta:
        get_latest_by = "id"


class Profile(models.Model):
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE)
    bio = models.TextField(blank=True)


class AuditLog(models.Model):
    message = models.CharField(max_length=200)


class SqliteNote(models.Model):
    text = models.CharField(max_length=50)


@receiver(post_save, sender=Employee)
def audit_employee(sender, instance, created, **kwargs):
    # A synchronous receiver that queries: runs natively inside asave().
    if created:
        AuditLog.objects.create(message=f"hired {instance.email}")


from asyncdjango.db.models import NativeManager, NativeModelMixin  # noqa: E402


class NativeOrganization(NativeModelMixin, Organization):
    """Opt-in native async ORM for projects with PATCH_ORM = False."""

    objects = NativeManager()

    class Meta:
        proxy = True
