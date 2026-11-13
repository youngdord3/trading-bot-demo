"""Test offline per binance_public.py (filtri batch multi-simbolo e fundingRate)."""
import unittest
from decimal import Decimal as D
from unittest.mock import patch

from binance_public import ALLOWED_PATHS, PublicAPIError, PublicClient


class TestBinancePublicExtensions(unittest.TestCase):
    def test_funding_rate_endpoint_is_whitelisted(self):
        self.assertIn("/fapi/v1/fundingRate", ALLOWED_PATHS)

    def test_disallowed_endpoint_raises(self):
        client = PublicClient()
        with self.assertRaises(PublicAPIError) as ctx:
            client.get("/fapi/v1/order")
        self.assertEqual(ctx.exception.code, "ENDPOINT_NOT_ALLOWED")

    def test_load_filters_many_parses_multiple_symbols_in_one_call(self):
        client = PublicClient()
        payload = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001", "maxQty": "100"},
                        {"filterType": "MIN_NOTIONAL", "notional": "50"},
                    ],
                },
                {
                    "symbol": "DOGEUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.00001"},
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "1000000"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
            ]
        }
        with patch.object(client, "get", return_value=payload) as mock_get:
            res = client.load_filters_many(["BTCUSDT", "DOGEUSDT"])
        mock_get.assert_called_once_with("/fapi/v1/exchangeInfo")
        self.assertEqual(set(res.keys()), {"BTCUSDT", "DOGEUSDT"})
        self.assertEqual(res["BTCUSDT"].min_notional, D("50"))
        self.assertEqual(res["DOGEUSDT"].market_step_size, D("1"))

    def test_load_filters_many_raises_when_symbol_missing_or_inactive(self):
        client = PublicClient()
        payload = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "BREAK",
                    "contractType": "PERPETUAL",
                    "filters": [],
                }
            ]
        }
        with patch.object(client, "get", return_value=payload):
            with self.assertRaises(PublicAPIError) as ctx:
                client.load_filters_many(["BTCUSDT"])
            self.assertEqual(ctx.exception.code, "SYMBOL_NOT_TRADABLE")

            with self.assertRaises(PublicAPIError) as ctx2:
                client.load_filters_many(["SOLUSDT"])
            self.assertEqual(ctx2.exception.code, "SYMBOL_NOT_UNIQUE")

    def test_get_funding_rates_returns_sorted_decimal_tuples(self):
        client = PublicClient()
        payload = [
            {"symbol": "BTCUSDT", "fundingTime": 2000, "fundingRate": "0.00025"},
            {"symbol": "BTCUSDT", "fundingTime": 1000, "fundingRate": "-0.00010"},
        ]
        with patch.object(client, "get", return_value=payload) as mock_get:
            rates = client.get_funding_rates("BTCUSDT", limit=10)
        mock_get.assert_called_once_with("/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "limit": 10})
        self.assertEqual(rates, [(1000, D("-0.00010")), (2000, D("0.00025"))])

    def test_get_funding_rates_validates_limit_and_payload(self):
        client = PublicClient()
        with self.assertRaises(PublicAPIError):
            client.get_funding_rates("BTCUSDT", limit=0)
        with patch.object(client, "get", return_value={"bad": True}):
            with self.assertRaises(PublicAPIError) as ctx:
                client.get_funding_rates("BTCUSDT", limit=5)
            self.assertEqual(ctx.exception.code, "INVALID_FUNDING_RESPONSE")


if __name__ == "__main__":
    unittest.main()
