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
		self.assertTrue(lease.take(["gpu-a", "gpu-b"], "MD-1"))

	def test_a_second_taker_is_refused_immediately(self):
		# The point: no waiting. A claim taken inside the winner's open transaction would block a rival.
		lease.take(["gpu-a"], "MD-1")
		self.assertFalse(lease.take(["gpu-a"], "MD-2"))

	def test_a_partial_lease_is_handed_back_whole(self):
		# gpu-b is gone, so the lease on gpu-a must not survive — a card marked busy that
		# nobody goes on to claim is a card stranded until its TTL.
		lease.take(["gpu-b"], "MD-1")
		self.assertFalse(lease.take(["gpu-a", "gpu-b"], "MD-2"))
		self.assertEqual(lease.leased(["gpu-a"]), set())

	def test_leased_reports_only_what_is_taken(self):
		lease.take(["gpu-b"], "MD-1")
		self.assertEqual(lease.leased(["gpu-a", "gpu-b", "gpu-c"]), {"gpu-b"})

	def test_release_frees_the_card_again(self):
		lease.take(["gpu-a"], "MD-1")
		lease.release(["gpu-a"])
		self.assertTrue(lease.take(["gpu-a"], "MD-2"))

	def test_two_cards_do_not_share_a_lease(self):
		# Card 0 of one box and card 0 of another are different records, so nothing has to say
		# which machine they are on — that is the point of keying on the card's own name.
		lease.take(["gpu-a"], "MD-1")
		self.assertTrue(lease.take(["gpu-b"], "MD-2"))

	def test_the_key_names_the_card(self):
		lease.take(["gpu-a"], "MD-1")
		self.assertEqual(next(iter(self.redis.store)), "grove:gpu_lease:gpu-a")


if __name__ == "__main__":
	unittest.main()
