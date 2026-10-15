"""Settings for asyncdjango's own test suite.

Set ASYNCDJANGO_DATABASE_URL to a PostgreSQL server where the test runner
may create and drop the test database (named test_<database>).
"""

import os
from urllib.parse import unquote, urlsplit

url = urlsplit(os.environ.get(
    "ASYNCDJANGO_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/asyncdjango"
))

SECRET_KEY = "asyncdjango-tests"
DEBUG = False
USE_TZ = True
TIME_ZONE = "America/Chicago"
ROOT_URLCONF = "tests.testapp.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ALLOWED_HOSTS = ["testserver"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": url.path.lstrip("/"),
        "USER": unquote(url.username or ""),
        "PASSWORD": unquote(url.password or ""),
        "HOST": url.hostname or "",
        "PORT": str(url.port or ""),
    },
    # A non-PostgreSQL alias exercises NativeAsyncUnsupported and fallback.
    "other": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
}
DATABASE_ROUTERS = ["tests.testapp.routers.OtherRouter"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "asyncdjango",
    "tests.testapp",
]
MIDDLEWARE = []
ASYNCDJANGO = {"POOL": {"min_size": 1, "max_size": 8}}
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
}
SILENCED_SYSTEM_CHECKS = ["asyncdjango.W001"]
