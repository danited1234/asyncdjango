"""The endpoints as classic synchronous DRF views, served over WSGI."""

from django.urls import path
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from benchmarks.bench.models import Employee, Organization


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
    def get(self, request, pk):
        organization = Organization.objects.get(pk=pk)
        total = Employee.objects.filter(organization=organization).count()
        employees = Employee.objects.select_related("organization").filter(
            organization=organization).order_by("id")[:20]
        data = EmployeeSerializer(employees, many=True).data
        return Response({"organization": organization.name, "total": total,
                         "employees": data})


class EmployeeCreate(APIView):
    def post(self, request, pk):
        organization = Organization.objects.get(pk=pk)
        serializer = EmployeeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(organization=organization)
        return Response(serializer.data, status=201)


class SlowQuery(APIView):
    def get(self, request):
        row = Organization.objects.extra(
            select={"slept": "pg_sleep(0.05)"}).order_by("id").first()
        return Response({"organization": row.name})


urlpatterns = [
    path("orgs/<int:pk>/", OrganizationDetail.as_view()),
    path("orgs/<int:pk>/employees/", EmployeeCreate.as_view()),
    path("slow/", SlowQuery.as_view()),
]
