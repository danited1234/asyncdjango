"""The same endpoint, written with ADRF or asyncdjango imports."""

from django.conf import settings
from django.urls import path

if settings.MODE == "native":
    from asyncdjango.rest_framework import serializers
    from asyncdjango.rest_framework.views import APIView
else:
    from adrf import serializers
    from adrf.views import APIView

from rest_framework.response import Response

from tests.testapp.models import Employee, Organization


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name"]


class EmployeeSerializer(serializers.ModelSerializer):
    organization = OrganizationSerializer()

    class Meta:
        model = Employee
        fields = ["id", "email", "salary", "active", "organization"]


class OrganizationDetail(APIView):
    """Typical CRM read: fetch, count, serialize a related page."""

    async def get(self, request, pk):
        organization = await Organization.objects.aget(pk=pk)
        total = await Employee.objects.filter(organization=organization).acount()
        employees = Employee.objects.select_related("organization").filter(
            organization=organization).order_by("id")[:20]
        data = await EmployeeSerializer(employees, many=True).adata
        return Response({"organization": organization.name, "total": total,
                         "employees": data})


class SlowQuery(APIView):
    """A request dominated by database time (50 ms)."""

    async def get(self, request):
        row = await Organization.objects.extra(select={"slept": "pg_sleep(0.05)"}).afirst()
        return Response({"organization": row.name})


urlpatterns = [
    path("orgs/<int:pk>/", OrganizationDetail.as_view()),
    path("slow/", SlowQuery.as_view()),
]
