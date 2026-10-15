"""Benchmark settings. BENCH_MODE=adrf (baseline) or native (asyncdjango)."""

import os
from urllib.parse import unquote, urlsplit

MODE = os.environ.get("BENCH_MODE", "native")
url = urlsplit(os.environ.get(
    "ASYNCDJANGO_BENCH_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/asyncdjango_bench",
))

SECRET_KEY = "bench"
DEBUG = False
USE_TZ = True
ROOT_URLCONF = "benchmarks.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ALLOWED_HOSTS = ["testserver"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": url.path.lstrip("/"),
        "USER": unquote(url.username or ""),
        "PASSWORD": unquote(url.password or ""),
        # An IP address: no DNS lookups in either mode.
        "HOST": "127.0.0.1" if url.hostname == "localhost" else url.hostname,
        "PORT": str(url.port or ""),
        # The CRM's production value; required for ADRF's thread-per-request
        # model (see myproject/settings.py).
        "CONN_MAX_AGE": 0,
    }
}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "tests.testapp",
]
if MODE == "native":
    INSTALLED_APPS.append("asyncdjango")
    ASYNCDJANGO = {"POOL": {"min_size": 10, "max_size": int(os.environ.get("BENCH_POOL", 20))}}
MIDDLEWARE = []
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
}
