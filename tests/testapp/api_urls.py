from django.urls import path

from asyncdjango.rest_framework.routers import DefaultRouter

from . import api

router = DefaultRouter()
router.register("organizations", api.OrganizationViewSet)

urlpatterns = [
    path("employees/", api.EmployeeList.as_view()),
    path("employees/<int:pk>/", api.EmployeeDetail.as_view()),
    path("me/", api.Me.as_view()),
    path("summary/", api.summary),
    *router.urls,
]
