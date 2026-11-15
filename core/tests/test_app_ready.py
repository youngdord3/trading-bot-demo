"""Test offline per common signing, binance_private offset/whitelist, app /ready e check_binance_account."""
import hashlib
import hmac
import unittest
from decimal import Decimal as D
from unittest.mock import patch

import app as app_mod
import check_binance_account as cba
from binance_private import ALLOWED_GET_PATHS, PrivateReadError, ReadOnlyClient
from common import build_signed_query
from strategy import UNIVERSE_SYMBOLS


class TestBaselineAndReady(unittest.TestCase):
    def test_build_signed_query_computes_expected_hmac(self):
        secret = b"my_super_secret_key_for_hmac_test"
        target = build_signed_query(secret, {"symbol": "BTCUSDT"}, now_ms=1_700_000_000_000, recv_window_ms=5000)
        query_part, sig_part = target.rsplit("&signature=", 1)
        expected = hmac.new(secret, query_part.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(sig_part, expected)
        self.assertIn("symbol=BTCUSDT", query_part)
        self.assertIn("timestamp=1700000000000", query_part)
        self.assertIn("recvWindow=5000", query_part)

    def test_readonly_client_blocks_disallowed_endpoint_and_syncs_offset(self):
        client = ReadOnlyClient(time_offset_ms=1500, api_key="k" * 20, api_secret="s" * 20)
        self.assertNotIn("/fapi/v1/order", ALLOWED_GET_PATHS)
        with self.assertRaises(PrivateReadError) as ctx:
            client.get("/fapi/v1/order")
        self.assertEqual(ctx.exception.args[0], "ENDPOINT_NOT_ALLOWED")

        client.set_time_offset_ms(29_780)
        self.assertEqual(client._time_offset_ms, 29_780)

    def test_app_evaluate_database_status_supports_v5_paused_and_v6_active(self):
        perms = {"db_user": "core_reader", "transaction_read_only": "on", "can_update_state": False}
        state_paused = {"environment": "DEMO", "status": "PAUSED", "orders_enabled": False, "updated_at": _DummyDt()}
        res_v5 = app_mod.evaluate_database_status(state_paused, [1, 2, 3, 4, 5], perms)
        self.assertTrue(res_v5["ready"])
        self.assertFalse(res_v5["trading_ready"])

        state_active = {"environment": "DEMO", "status": "ACTIVE", "orders_enabled": True, "updated_at": _DummyDt()}
        res_v6 = app_mod.evaluate_database_status(state_active, [1, 2, 3, 4, 5, 6], perms)
        self.assertTrue(res_v6["ready"])
        self.assertTrue(res_v6["trading_ready"])

        state_halted = {"environment": "DEMO", "status": "HALTED", "orders_enabled": False, "updated_at": _DummyDt()}
        self.assertFalse(app_mod.evaluate_database_status(state_halted, [1, 2, 3, 4, 5, 6], perms)["ready"])

        bad_state = {"environment": "DEMO", "status": "PAUSED", "orders_enabled": True, "updated_at": _DummyDt()}
        self.assertFalse(app_mod.evaluate_database_status(bad_state, [1, 2, 3, 4, 5, 6], perms)["ready"])

    def test_check_binance_account_validates_all_12_symbols_isolated_leverage(self):
        sym_configs = [
            {"symbol": s, "marginType": "ISOLATED", "leverage": 7}
            for s in UNIVERSE_SYMBOLS
        ]
        res_ok = cba.evaluate_account_snapshot(
            account_data={"totalMarginBalance": "5816.60", "availableBalance": "5816.60"},
            dual_data={"dualSidePosition": False},
            sym_configs=sym_configs,
            positions=[],
            open_orders=[],
            algo_orders=[],
            target_leverage=7,
        )
        self.assertTrue(res_ok["passed"])
        self.assertEqual(res_ok["checks"]["all_symbols_isolated"], True)
        self.assertEqual(res_ok["checks"]["all_symbols_target_leverage"], True)
        self.assertEqual(res_ok["checks"]["leverage_supports_combined_exposure"], True)

        # Leva 2x insufficiente per esposizione combinata V2 (3 * 2.0 * equity = 6.0x)
        res_low_lev = cba.evaluate_account_snapshot(
            account_data={"totalMarginBalance": "5816.60", "availableBalance": "5816.60"},
            dual_data={"dualSidePosition": False},
            sym_configs=[{"symbol": s, "marginType": "ISOLATED", "leverage": 2} for s in UNIVERSE_SYMBOLS],
            positions=[],
            open_orders=[],
            algo_orders=[],
            target_leverage=2,
        )
        self.assertFalse(res_low_lev["passed"])
        self.assertFalse(res_low_lev["checks"]["leverage_supports_combined_exposure"])

        sym_configs[1]["marginType"] = "CROSSED"
        sym_configs[2]["leverage"] = 20
        res_bad = cba.evaluate_account_snapshot(
            account_data={"totalMarginBalance": "5816.60", "availableBalance": "5816.60"},
            dual_data={"dualSidePosition": False},
            sym_configs=sym_configs,
            positions=[],
            open_orders=[],
            algo_orders=[],
            target_leverage=7,
        )
        self.assertFalse(res_bad["passed"])
        self.assertIn(UNIVERSE_SYMBOLS[1], res_bad["non_isolated_symbols"])
        self.assertIn(UNIVERSE_SYMBOLS[2], res_bad["wrong_leverage_symbols"])

    def test_parse_binance_json_allows_success_code_200_and_blocks_errors(self):
        from common import parse_binance_json

        ok = parse_binance_json(200, b'{"code": 200, "msg": "success"}', 1024, PrivateReadError)
        self.assertEqual(ok, {"code": 200, "msg": "success"})

        with self.assertRaises(PrivateReadError) as err_ctx:
            parse_binance_json(200, b'{"code": -1021, "msg": "Timestamp out of window"}', 1024, PrivateReadError)
        self.assertEqual(err_ctx.exception.args[0], "BINANCE_CODE_-1021")


class _DummyDt:
    def isoformat(self) -> str:
        return "2026-11-15T18:00:00+00:00"


if __name__ == "__main__":
    unittest.main()
