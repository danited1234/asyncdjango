from django.db import models


class Organization(models.Model):
    name = models.CharField(max_length=100, unique=True)


class Employee(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="employees"
    )
    email = models.EmailField(unique=True)
    salary = models.DecimalField(max_digits=10, decimal_places=2)
    active = models.BooleanField(default=True)
