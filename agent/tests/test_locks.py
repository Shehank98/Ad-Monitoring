import threading
import unittest
from datetime import timedelta

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from agent.locks import ScopeBusy, fallback_lock, scope_lock, take_xact_lock
from agent.models import ScopeLockRow
from agent.scope import lock_key

PG = connection.vendor == 'postgresql'


class LockKeyTest(unittest.TestCase):
    def test_signed_64_bit_and_stable(self):
        k = lock_key(1, 'Sirasa TV', 'January 2025')
        self.assertEqual(k, lock_key(1, 'Sirasa TV', 'January 2025'))
        self.assertTrue(-2 ** 63 <= k < 2 ** 63)
        self.assertNotEqual(k, lock_key(1, 'sirasa tv', 'January 2025'))   # exact strings


@unittest.skipIf(PG, 'fallback lock is for non-PostgreSQL databases')
class FallbackLockTest(TransactionTestCase):
    def test_contention_and_release(self):
        with fallback_lock(42):
            self.assertTrue(ScopeLockRow.objects.filter(key=42).exists())
            with self.assertRaises(ScopeBusy):
                with fallback_lock(42):
                    pass
        self.assertFalse(ScopeLockRow.objects.filter(key=42).exists())

    def test_released_in_finally_on_error(self):
        with self.assertRaises(ValueError):
            with fallback_lock(7):
                raise ValueError('boom')
        self.assertFalse(ScopeLockRow.objects.filter(key=7).exists())

    def test_expired_row_is_free(self):
        now = timezone.now()
        ScopeLockRow.objects.create(key=9, owner='dead-worker', acquired_at=now - timedelta(hours=1),
                                    expires_at=now - timedelta(minutes=1))
        with fallback_lock(9):
            self.assertNotEqual(ScopeLockRow.objects.get(key=9).owner, 'dead-worker')

    def test_refuses_inside_atomic(self):
        with transaction.atomic():
            with self.assertRaises(RuntimeError):
                with fallback_lock(1):
                    pass


@unittest.skipUnless(PG, 'PostgreSQL advisory lock')
class AdvisoryLockTest(TransactionTestCase):
    def test_contention_between_connections_and_release_on_rollback(self):
        key = lock_key(99, 'X', 'Y')
        results = {}
        held, done = threading.Event(), threading.Event()

        def other():
            conn = connections.create_connection('default')
            try:
                conn.set_autocommit(False)
                with conn.cursor() as cur:
                    held.wait(5)
                    cur.execute('SELECT pg_try_advisory_xact_lock(%s)', [key])
                    results['while_held'] = cur.fetchone()[0]
                conn.rollback()
                done.wait(5)
                with conn.cursor() as cur:
                    cur.execute('SELECT pg_try_advisory_xact_lock(%s)', [key])
                    results['after_release'] = cur.fetchone()[0]
                conn.rollback()
            finally:
                conn.close()

        t = threading.Thread(target=other)
        t.start()
        try:
            with transaction.atomic():
                take_xact_lock(key)
                held.set()
                t.join(0.5)
                import time
                time.sleep(0.5)
                transaction.set_rollback(True)
        finally:
            done.set()
            t.join(5)
        self.assertFalse(results['while_held'])
        self.assertTrue(results['after_release'])

    def test_scope_lock_requires_atomic_and_nests(self):
        with self.assertRaises(RuntimeError):
            take_xact_lock(1)
        with scope_lock(123):
            self.assertTrue(connection.in_atomic_block)
