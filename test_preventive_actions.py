import unittest
import sqlite3
from BNB import get_active_symbols, claim_symbol_for_instance, init_db, get_conn

class TestPreventiveActions(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_symbol_mutual_exclusion(self):
        # Instance 1 claims ETHUSDT
        with get_conn() as conn:
            conn.execute("UPDATE bot_instances SET current_symbol = 'ETHUSDT', status = 'RUNNING' WHERE id = 1")
            conn.execute("UPDATE bot_instances SET current_symbol = NULL, status = 'CREATED' WHERE id = 2")

        active = get_active_symbols(exclude_instance_id=2)
        self.assertIn("ETHUSDT", active)

        # Instance 2 must NOT be able to claim ETHUSDT
        claimed = claim_symbol_for_instance(2, "ETHUSDT")
        self.assertFalse(claimed, "Instance 2 should not be able to claim ETHUSDT when Instance 1 is holding it!")

        # Instance 2 should be able to claim a different symbol like AVAXUSDT
        claimed_diff = claim_symbol_for_instance(2, "AVAXUSDT")
        self.assertTrue(claimed_diff, "Instance 2 should be able to claim an untaken symbol!")

        # Cleanup
        with get_conn() as conn:
            conn.execute("UPDATE bot_instances SET current_symbol = NULL, status = 'STOPPED' WHERE id IN (1, 2)")

if __name__ == "__main__":
    unittest.main()
