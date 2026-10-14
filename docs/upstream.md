# Upstream source policy

The compatibility reference is the Django **6.0.8** source tag:
https://github.com/django/django/tree/6.0.8. This is a target for API and
behavior comparisons, not a claim that Asyncdjango implements Django 6.0.8.

No Django source code has been copied into this repository. If an upstream
implementation is reused, record its exact tag and commit SHA, original file
path, local destination, and any modifications in this document before merging.
Retain the upstream copyright notice, its BSD three-clause LICENSE, and any
additional notices applicable to that file (including LICENSE.python where
relevant). Include those license files in source and binary distributions.
Review each third-party dependency separately. This is a project compliance
checklist, not legal advice.

The core design constraint remains native awaited PostgreSQL I/O. Reusing
Django's synchronous database execution layer would violate that constraint;
source reuse must be isolated from that layer and tested against the no-thread
guard and live PostgreSQL integration suite.
