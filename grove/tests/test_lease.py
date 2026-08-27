import unittest
from unittest.mock import patch
import frappe
from grove.placement import lease


class FakeRedis:
	"""Only the three operations a lease uses. `set(nx=True)` returns None when the key exists,
	which is what makes the lease atomic."""

	def __init__(self):
		self.store = {}

	def set(self, key, val, nx=False, ex=None):
		if nx and key in self.store:
			return None
		self.store[key] = val
		return True

	def get(self, key):
		return self.store.get(key)

	def exists(self, *names, **kwargs):
		# RedisWrapper overrides exists() to apply make_key, while set()/get()/delete() stay raw.
		# Mixing them writes and reads different keys, so the lease must never call this.
		raise AssertionError("lease must not use exists() — see leased()")

	def delete(self, key):
		self.store.pop(key, None)


class TestTheLeaseAnnouncesIntent(unittest.TestCase):
	def setUp(self):
		self.redis = FakeRedis()
		patcher = patch.object(frappe, "cache", self.redis)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_free_card_can_be_taken(self):
		self.assertTrue(lease.take("mc-x", [0, 1], "MD-1"))

	def test_a_second_taker_is_refused_immediately(self):
		# The point: no waiting. A GPU Claim insert would block on the winner's open transaction.
		lease.take("mc-x", [0], "MD-1")
		self.assertFalse(lease.take("mc-x", [0], "MD-2"))

	def test_a_partial_lease_is_handed_back_whole(self):
		# Card 1 is gone, so the lease on card 0 must not survive — a card marked busy that
		# nobody goes on to claim is a card stranded until its TTL.
		lease.take("mc-x", [1], "MD-1")
		self.assertFalse(lease.take("mc-x", [0, 1], "MD-2"))
		self.assertEqual(lease.leased("mc-x", [0]), set())

	def test_leased_reports_only_what_is_taken(self):
		lease.take("mc-x", [1], "MD-1")
		self.assertEqual(lease.leased("mc-x", [0, 1, 2]), {1})

	def test_release_frees_the_card_again(self):
		lease.take("mc-x", [0], "MD-1")
		lease.release("mc-x", [0])
		self.assertTrue(lease.take("mc-x", [0], "MD-2"))

	def test_machines_do_not_share_a_lease(self):
		lease.take("mc-x", [0], "MD-1")
		self.assertTrue(lease.take("mc-y", [0], "MD-2"))

	def test_the_key_names_the_machine_and_the_card(self):
		lease.take("mc-x", [0], "MD-1")
		self.assertEqual(next(iter(self.redis.store)), "grove:gpu_lease:mc-x:0")


if __name__ == "__main__":
	unittest.main()
