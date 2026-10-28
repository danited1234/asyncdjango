from django.contrib.sessions.backends.base import CreateError
from django.contrib.sessions.backends.cached_db import SessionStore as CachedDBStore
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session

from asyncdjango.db.patch import is_native

from .test_orm import OffloadGuardedTestCase


class SessionStoreTests(OffloadGuardedTestCase):
    def test_db_asave_is_native(self):
        self.assertTrue(is_native(SessionStore.asave))

    async def test_asave_creates_and_updates_without_a_thread(self):
        store = SessionStore()
        await store.aset("cart", [1, 2])
        await store.asave()
        self.assertIsNotNone(store.session_key)

        loaded = SessionStore(session_key=store.session_key)
        self.assertEqual(await loaded.aget("cart"), [1, 2])
        await loaded.aset("cart", [3])
        await loaded.asave()

        again = SessionStore(session_key=store.session_key)
        self.assertEqual(await again.aget("cart"), [3])
        self.assertEqual(await Session.objects.acount(), 1)

    async def test_must_create_on_existing_key_raises_create_error(self):
        store = SessionStore()
        await store.acreate()
        duplicate = SessionStore(session_key=store.session_key)
        with self.assertRaises(CreateError):
            await duplicate.asave(must_create=True)

    async def test_cycle_key_replaces_the_row(self):
        store = SessionStore()
        await store.aset("user", 7)
        await store.asave()
        old_key = store.session_key
        await store.acycle_key()
        self.assertNotEqual(store.session_key, old_key)
        self.assertFalse(await store.aexists(old_key))
        self.assertEqual(await SessionStore(session_key=store.session_key).aget("user"), 7)

    async def test_cached_db_saves_to_the_database(self):
        store = CachedDBStore()
        await store.aset("theme", "dark")
        await store.asave()
        row = await Session.objects.aget(session_key=store.session_key)
        self.assertEqual(SessionStore().decode(row.session_data), {"theme": "dark"})
