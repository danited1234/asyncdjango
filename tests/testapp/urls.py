from django.urls import include, path

from . import views

urlpatterns = [
    path("orgs/", views.organizations),
    path("create/", views.create_then_fail),
    path("missing/", views.missing),
    path("slow/", views.slow_write),
    path("deferred/", views.deferred),
    path("api/", include("tests.testapp.api_urls")),
]
