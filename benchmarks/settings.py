"""Benchmark settings for one stack, chosen by BENCH_STACK (see benchmarks/stacks.py)."""

import os
from urllib.parse import unquote, urlsplit

from benchmarks.stacks import STACKS

STACK = STACKS[os.environ.get("BENCH_STACK", "asyncdjango-drf")]
POOL_SIZE = int(os.environ.get("BENCH_POOL", 20))
url = urlsplit(os.environ.get(
    "ASYNCDJANGO_BENCH_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/asyncdjango_bench",
))

SECRET_KEY = "bench"
DEBUG = False
USE_TZ = True
ROOT_URLCONF = STACK.urlconf
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ALLOWED_HOSTS = ["testserver", "127.0.0.1", "localhost"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": url.path.lstrip("/"),
        "USER": unquote(url.username or ""),
        "PASSWORD": unquote(url.password or ""),
        # An IP address: no DNS lookups in any stack.
        "HOST": "127.0.0.1" if url.hostname == "localhost" else url.hostname,
        "PORT": str(url.port or ""),
        "OPTIONS": {},
    }
}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "benchmarks.bench",
]
if STACK.native:
    INSTALLED_APPS.append("asyncdjango")
    ASYNCDJANGO = {"POOL": {"min_size": 4, "max_size": POOL_SIZE}}
else:
    # Every stack gets a pool of the same size. For Django this is its
    # built-in psycopg pool (Django 5.1+), the recommended setup under ASGI.
    DATABASES["default"]["OPTIONS"]["pool"] = {"min_size": 4, "max_size": POOL_SIZE}
MIDDLEWARE = []
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
}
