"""The endpoints as async DRF-style views: ADRF, or asyncdjango.rest_framework.

Only the imports and the validation call differ between the two stacks.
"""

from django.conf import settings
from django.urls import path
from rest_framework.response import Response

from benchmarks.bench.models import Employee, Organization

if settings.STACK.native:
    from asyncdjango.rest_framework import serializers
    from asyncdjango.rest_framework.views import APIView

    async def validate(serializer):
        await serializer.ais_valid(raise_exception=True)
else:
    from adrf import serializers
    from adrf.views import APIView
    from asgiref.sync import sync_to_async

    async def validate(serializer):
        # ADRF has no async is_valid(), and the unique email check queries.
        await sync_to_async(serializer.is_valid)(raise_exception=True)


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name"]


class EmployeeSerializer(serializers.ModelSerializer):
    organization = OrganizationSerializer(read_only=True)

    class Meta:
        model = Employee
        fields = ["id", "email", "salary", "active", "organization"]


class OrganizationDetail(APIView):
    """Read: fetch a row, count its children, serialize a page of them."""

    async def get(self, request, pk):
        organization = await Organization.objects.aget(pk=pk)
        total = await Employee.objects.filter(organization=organization).acount()
        employees = Employee.objects.select_related("organization").filter(
            organization=organization).order_by("id")[:20]
        data = await EmployeeSerializer(employees, many=True).adata
        return Response({"organization": organization.name, "total": total,
                         "employees": data})


class EmployeeCreate(APIView):
    """Write: validate (including a uniqueness query) and insert."""

    async def post(self, request, pk):
        organization = await Organization.objects.aget(pk=pk)
        serializer = EmployeeSerializer(data=request.data)
        await validate(serializer)
        await serializer.asave(organization=organization)
        return Response(await serializer.adata, status=201)


class SlowQuery(APIView):
    """I/O-bound: one query that waits 50 ms in PostgreSQL."""

    async def get(self, request):
        row = await Organization.objects.extra(
            select={"slept": "pg_sleep(0.05)"}).order_by("id").afirst()
        return Response({"organization": row.name})


urlpatterns = [
    path("orgs/<int:pk>/", OrganizationDetail.as_view()),
    path("orgs/<int:pk>/employees/", EmployeeCreate.as_view()),
    path("slow/", SlowQuery.as_view()),
]
