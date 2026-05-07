import os
import unittest

from src.data.twelve_data_client import TwelveDataClient


class TwelveDataClientRotationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_multi = os.environ.get("TWELVE_DATA_API_KEYS")
        self._old_single = os.environ.get("TWELVE_DATA_API_KEY")
        self._old_limit = os.environ.get("MAX_REQUESTS_PER_DAY")
        self._old_delay = os.environ.get("REQUEST_DELAY_SECONDS")

        os.environ["REQUEST_DELAY_SECONDS"] = "0"

    def tearDown(self):
        if self._old_multi is None:
            os.environ.pop("TWELVE_DATA_API_KEYS", None)
        else:
            os.environ["TWELVE_DATA_API_KEYS"] = self._old_multi

        if self._old_single is None:
            os.environ.pop("TWELVE_DATA_API_KEY", None)
        else:
            os.environ["TWELVE_DATA_API_KEY"] = self._old_single

        if self._old_limit is None:
            os.environ.pop("MAX_REQUESTS_PER_DAY", None)
        else:
            os.environ["MAX_REQUESTS_PER_DAY"] = self._old_limit

        if self._old_delay is None:
            os.environ.pop("REQUEST_DELAY_SECONDS", None)
        else:
            os.environ["REQUEST_DELAY_SECONDS"] = self._old_delay

    async def test_loads_multiple_keys_from_env(self):
        os.environ["TWELVE_DATA_API_KEYS"] = "key-a, key-b, key-a"
        os.environ.pop("TWELVE_DATA_API_KEY", None)

        client = TwelveDataClient()

        self.assertTrue(client.enabled)
        self.assertEqual(client.api_keys, ["key-a", "key-b"])
        self.assertEqual(client.get_rate_limit_status()["configured_keys"], 2)

    async def test_rotates_when_first_key_hits_quota_error(self):
        os.environ["TWELVE_DATA_API_KEYS"] = "key-a,key-b"
        os.environ["MAX_REQUESTS_PER_DAY"] = "2"

        client = TwelveDataClient()
        seen_keys = []

        async def fake_get(url, params):
            seen_keys.append(params["apikey"])
            if params["apikey"] == "key-a":
                return {
                    "status": "error",
                    "code": 429,
                    "message": "API credits exhausted for today",
                }
            return {
                "values": [
                    {
                        "datetime": "2026-05-07 00:00:00",
                        "open": "1.1",
                        "high": "1.2",
                        "low": "1.0",
                        "close": "1.15",
                        "volume": "0",
                    }
                ]
            }

        client._perform_http_get = fake_get

        data = await client.get_intraday_data("EUR/USD")

        self.assertFalse(data.empty)
        self.assertEqual(seen_keys, ["key-a", "key-b"])

        status = client.get_rate_limit_status()
        self.assertEqual(status["requests_today"], 3)
        self.assertEqual(status["per_key"][0]["remaining"], 0)
        self.assertEqual(status["per_key"][1]["requests_today"], 1)

    async def test_uses_next_key_when_local_daily_limit_is_reached(self):
        os.environ["TWELVE_DATA_API_KEYS"] = "key-a,key-b"
        os.environ["MAX_REQUESTS_PER_DAY"] = "1"

        client = TwelveDataClient()
        calls = []

        async def fake_get(url, params):
            calls.append(params["apikey"])
            return {"symbol": "EUR/USD", "price": "1.10"}

        client._perform_http_get = fake_get

        await client.get_quote("EUR/USD")
        await client.get_quote("EUR/USD")

        self.assertEqual(calls, ["key-a", "key-b"])


if __name__ == "__main__":
    unittest.main()
