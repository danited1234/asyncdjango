from django.urls import include, path

from asyncdjango import views as asyncdjango_views

from . import views

urlpatterns = [
    path("orgs/", views.organizations),
    path("create/", views.create_then_fail),
    path("missing/", views.missing),
    path("slow/", views.slow_write),
    path("deferred/", views.deferred),
    path("blocking/", views.blocking),
    path("health/", asyncdjango_views.health),
    path("health/quick/", asyncdjango_views.health, {"timeout": 0.2}),
    path("metrics/", asyncdjango_views.metrics),
    path("api/", include("tests.testapp.api_urls")),
]
