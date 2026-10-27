"""DRF code written the way production ADRF projects write it, on asyncdjango.rest_framework."""

from asgiref.sync import async_to_sync
from rest_framework import exceptions, permissions, throttling
from rest_framework.authentication import BaseAuthentication
from rest_framework.filters import OrderingFilter
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from asyncdjango.rest_framework import generics, serializers, viewsets
from asyncdjango.rest_framework.decorators import api_view
from asyncdjango.rest_framework.views import APIView

from .models import AuditLog, Employee, Organization, Tag


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name"]


class EmployeeSerializer(serializers.ModelSerializer):
    organization_name = serializers.SerializerMethodField()
    report_count = serializers.SerializerMethodField()
    tags = serializers.PrimaryKeyRelatedField(many=True, queryset=Tag.objects.all(),
                                              required=False)

    class Meta:
        model = Employee
        fields = ["id", "email", "organization", "organization_name", "report_count",
                  "tags", "salary"]

    def get_organization_name(self, employee):
        # Lazy relation read: an awaited query on the native path.
        return employee.organization.name

    async def get_report_count(self, employee):
        return await employee.reports.acount()


class StaffUser:
    """Stand-in for a custom user model returned by JWT authentication."""

    is_authenticated = True

    def __init__(self, organization):
        self.organization = organization


class AsyncHelperJWTAuthentication(BaseAuthentication):
    """A common JWT pattern: sync authenticate() wrapping an async helper
    with async_to_sync."""

    def authenticate(self, request):
        return async_to_sync(self._authenticate_async)(request)

    async def _authenticate_async(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        try:
            organization = await Organization.objects.aget(name=header.split(" ", 1)[1])
        except Organization.DoesNotExist:
            raise exceptions.AuthenticationFailed("User not found")
        return StaffUser(organization), header

    def authenticate_header(self, request):
        return "Bearer"


class AsyncIsStaff(permissions.BasePermission):
    async def has_permission(self, request, view):
        return request.user is not None and await Organization.objects.filter(
            pk=request.user.organization.pk).aexists()


class LoggingThrottle(throttling.BaseThrottle):
    """A synchronous throttle that queries the database."""

    def allow_request(self, request, view):
        AuditLog.objects.create(message=f"throttle {request.method}")
        return request.headers.get("X-Throttle") != "deny"

    def wait(self):
        return 7


class Pages(PageNumberPagination):
    page_size = 2


class OrganizationViewSet(viewsets.ModelViewSet):
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    pagination_class = Pages
    filter_backends = [OrderingFilter]
    ordering_fields = ["name"]


class EmployeeDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = Employee.objects.all()
    serializer_class = EmployeeSerializer


class EmployeeList(generics.ListCreateAPIView):
    queryset = Employee.objects.order_by("id")
    serializer_class = EmployeeSerializer


class Me(APIView):
    authentication_classes = [AsyncHelperJWTAuthentication]
    permission_classes = [AsyncIsStaff]
    throttle_classes = [LoggingThrottle]

    async def get(self, request):
        data = await OrganizationSerializer(request.user.organization).adata
        return Response({"organization": data})


@api_view(["GET"])
async def summary(request):
    return Response({"organizations": await Organization.objects.acount()})
