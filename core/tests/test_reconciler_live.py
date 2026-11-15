"""Test offline TDD per reconciler.py in modalita' live 006 e legacy."""
import unittest
from decimal import Decimal as D

import reconciler as rec


class FakeReconcilerClient:
    def __init__(self, *, account=None, positions=None, orders=None, algo=None, dual=None):
        self.account = account if account is not None else {"totalMarginBalance": "5816.60"}
        self.positions = positions if positions is not None else []
        self.orders = orders if orders is not None else []
        self.algo = algo if algo is not None else []
        self.dual = dual if dual is not None else {"dualSidePosition": False}
        self.sync_calls = 0

    def sync_time_offset(self):
        self.sync_calls += 1
        return 29780

    def get(self, path, params=None):
        mapping = {
            "/fapi/v3/account": self.account,
            "/fapi/v3/positionRisk": self.positions,
            "/fapi/v1/openOrders": self.orders,
            "/fapi/v1/openAlgoOrders": self.algo,
            "/fapi/v1/positionSide/dual": self.dual,
        }
        return mapping[path]


class FakeReconcilerCursor:
    def __init__(self, *, has_live_tables=True, status="ACTIVE", orders_enabled=True,
                 live_positions=None, pending_orders=None):
        self.has_live_tables = has_live_tables
        self.status = status
        self.orders_enabled = orders_enabled
        self.live_positions = live_positions or []
        self.pending_orders = pending_orders or []
        self.snapshots = []
        self.tripped = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        params = params or ()
        self._rows = []
        if "to_regclass('bot.live_positions')" in norm:
            self._rows = [(self.has_live_tables,)]
            return
        if "FROM bot.system_state" in norm:
            self._rows = [(self.status, self.orders_enabled)]
            return
        if "FROM bot.live_positions" in norm:
            self._rows = list(self.live_positions)
            return
        if "FROM bot.orders" in norm:
            self._rows = list(self.pending_orders)
            return
        if "INSERT INTO bot.reconciliation_snapshots" in norm:
            self.snapshots.append(params)
            self._rows = [(101,)]
            return
        if "bot.record_reconciliation_anomaly" in norm:
            self.snapshots.append(params)
            self._rows = [(202,)]
            return
        if "bot.trip_circuit_breaker" in norm:
            self.tripped.append(params)
            self._rows = [(True,)]
            return
        raise AssertionError(f"Unexpected SQL: {norm}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeReconcilerConn:
    def __init__(self, cursor_obj):
        self._cursor = cursor_obj

    def cursor(self):
        return self._cursor

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestReconcilerLive(unittest.TestCase):
    def test_legacy_single_arg_evaluate_backward_compatible(self):
        self.assertIsNone(rec.evaluate({
            "position_mode": "ONE_WAY",
            "open_positions_count": 0,
            "open_orders_count": 0,
            "open_algo_orders_count": 0,
        }))
        self.assertEqual(rec.evaluate({
            "position_mode": "ONE_WAY",
            "open_positions_count": 1,
            "open_orders_count": 0,
            "open_algo_orders_count": 0,
        }), "UNEXPECTEDPOSITIONS")
        self.assertEqual(rec.evaluate({
            "position_mode": "HEDGE",
            "open_positions_count": 0,
            "open_orders_count": 0,
            "open_algo_orders_count": 0,
        }), "UNEXPECTEDHEDGEMODE")

    def test_live_matching_positions_and_algo_stop_passes_ok(self):
        client = FakeReconcilerClient(
            positions=[{"symbol": "BTCUSDT", "positionAmt": "0.015"}],
            algo=[{"symbol": "BTCUSDT", "algoId": 777001, "clientAlgoId": "s_btc_1", "algoStatus": "NEW"}],
        )
        cur = FakeReconcilerCursor(
            live_positions=[("BTCUSDT", "LONG", D("0.015"), 777001, "s_btc_1", "NEW")],
        )
        conn = FakeReconcilerConn(cur)
        consecutive, out = rec.cycle(client, conn, 0, 3)
        self.assertEqual(consecutive, 0)
        self.assertEqual(out["result"], "OK")
        self.assertEqual(len(cur.snapshots), 1)

    def test_ghost_exchange_position_triggers_desync_positions_and_trips(self):
        client = FakeReconcilerClient(
            positions=[{"symbol": "ETHUSDT", "positionAmt": "-0.5"}],
        )
        cur = FakeReconcilerCursor(live_positions=[])
        conn = FakeReconcilerConn(cur)

        consecutive, out = rec.cycle(client, conn, 2, 3)
        self.assertEqual(consecutive, 3)
        self.assertEqual(out["result"], "ANOMALY")
        self.assertEqual(out["error_code"], "DESYNCPOSITIONS")
        self.assertTrue(out["tripped"])
        self.assertRegex(out["error_code"], r"^[A-Z0-9]{1,64}$")

    def test_missing_or_mismatched_algo_stop_flags_desync_algo_orders(self):
        client = FakeReconcilerClient(
            positions=[{"symbol": "BTCUSDT", "positionAmt": "0.015"}],
            algo=[],  # stop mancante su exchange
        )
        cur = FakeReconcilerCursor(
            live_positions=[("BTCUSDT", "LONG", D("0.015"), 777001, "s_btc_1", "NEW")],
        )
        conn = FakeReconcilerConn(cur)
        consecutive, out = rec.cycle(client, conn, 0, 3)
        self.assertEqual(consecutive, 1)
        self.assertEqual(out["error_code"], "DESYNCALGOORDERS")

    def test_recent_pending_order_grace_vs_stale_pending_order(self):
        client = FakeReconcilerClient(
            positions=[{"symbol": "SOLUSDT", "positionAmt": "5"}],
        )
        # In-flight < 120s: tollerato
        cur_ok = FakeReconcilerCursor(
            live_positions=[],
            pending_orders=[("m_OL_SOLUSDT_1", "SOLUSDT", "PENDING", 15.0)],
        )
        consecutive, out = rec.cycle(client, FakeReconcilerConn(cur_ok), 0, 3)
        self.assertEqual(out["result"], "OK")

        # Stale > 120s: anomalia STALEPENDINGORDERS
        cur_stale = FakeReconcilerCursor(
            live_positions=[],
            pending_orders=[("m_OL_SOLUSDT_1", "SOLUSDT", "PENDING", 180.0)],
        )
        consecutive, out = rec.cycle(client, FakeReconcilerConn(cur_stale), 0, 3)
        self.assertEqual(out["result"], "ANOMALY")
        self.assertEqual(out["error_code"], "STALEPENDINGORDERS")


if __name__ == "__main__":
    unittest.main()
