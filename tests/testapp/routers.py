class OtherRouter:
    """Send the SqliteNote model to the non-PostgreSQL 'other' alias."""

    def _alias(self, model):
        return "other" if model._meta.model_name == "sqlitenote" else None

    def db_for_read(self, model, **hints):
        return self._alias(model)

    def db_for_write(self, model, **hints):
        return self._alias(model)

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if model_name == "sqlitenote":
            return db == "other"
        return db == "default"
