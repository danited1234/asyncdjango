"""The endpoints as plain Django async views (no DRF).

The JSON bodies match the DRF views byte for byte, so every stack's
responses can be compared with one fingerprint.
"""

import json
from decimal import Decimal

from django.http import JsonResponse
from django.urls import path

from benchmarks.bench.models import Employee, Organization

COMPACT = {"separators": (",", ":"), "ensure_ascii": False}


def employee_json(employee):
    return {"id": employee.pk, "email": employee.email, "salary": str(employee.salary),
            "active": employee.active,
            "organization": {"id": employee.organization.pk,
                             "name": employee.organization.name}}


async def organization_detail(request, pk):
    organization = await Organization.objects.aget(pk=pk)
    total = await Employee.objects.filter(organization=organization).acount()
    employees = Employee.objects.select_related("organization").filter(
        organization=organization).order_by("id")[:20]
    data = [employee_json(employee) async for employee in employees]
    return JsonResponse({"organization": organization.name, "total": total,
                         "employees": data}, json_dumps_params=COMPACT)


async def employee_create(request, pk):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    organization = await Organization.objects.aget(pk=pk)
    payload = json.loads(request.body)
    if await Employee.objects.filter(email=payload["email"]).aexists():
        return JsonResponse({"email": ["employee with this email already exists."]},
                            status=400)
    employee = await Employee.objects.acreate(
        organization=organization, email=payload["email"],
        salary=Decimal(payload["salary"]).quantize(Decimal("0.01")))
    return JsonResponse(employee_json(employee), status=201, json_dumps_params=COMPACT)


async def slow_query(request):
    row = await Organization.objects.extra(
        select={"slept": "pg_sleep(0.05)"}).order_by("id").afirst()
    return JsonResponse({"organization": row.name}, json_dumps_params=COMPACT)


urlpatterns = [
    path("orgs/<int:pk>/", organization_detail),
    path("orgs/<int:pk>/employees/", employee_create),
    path("slow/", slow_query),
]
