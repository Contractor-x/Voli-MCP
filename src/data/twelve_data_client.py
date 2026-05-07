"""
Twelve Data API client for forex market data.
Handles rate limiting, error handling, and data formatting.
Uses async httpx for non-blocking HTTP calls.
"""

import os
import asyncio
import httpx
from typing import Dict, Optional, Any, List, Tuple
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas as pd
import pytz

# Load environment variables
load_dotenv()


class TwelveDataClient:
    """Async client for Twelve Data API with per-key rotation."""

    BASE_URL = "https://api.twelvedata.com"

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Twelve Data client.

        Args:
            api_key: API key (defaults to env vars TWELVE_DATA_API_KEYS/TWELVE_DATA_API_KEY)
        """
        self.api_keys = self._load_api_keys(api_key)
        self.enabled = bool(self.api_keys)

        # Rate limiting is applied per key.
        self.max_requests_per_day = int(os.getenv("MAX_REQUESTS_PER_DAY", "800"))
        self.request_delay = float(os.getenv("REQUEST_DELAY_SECONDS", "1.0"))
        self.current_key_index = 0
        self.key_states = [self._build_key_state(key) for key in self.api_keys]

    @property
    def api_key(self) -> Optional[str]:
        """Expose the currently selected key for backward compatibility."""
        if not self.api_keys:
            return None
        return self.key_states[self.current_key_index]["api_key"]

    @staticmethod
    def _load_api_keys(api_key: Optional[str]) -> List[str]:
        """Load one or many Twelve Data API keys from config."""
        raw_keys = api_key or os.getenv("TWELVE_DATA_API_KEYS") or os.getenv("TWELVE_DATA_API_KEY", "")
        keys = [key.strip() for key in raw_keys.split(",") if key.strip()]

        # Support newline-separated env values for hosts that make comma lists awkward.
        if len(keys) <= 1 and "\n" in raw_keys:
            keys = [key.strip() for key in raw_keys.splitlines() if key.strip()]

        # Deduplicate while keeping the original order.
        return list(dict.fromkeys(keys))

    @staticmethod
    def _build_key_state(api_key: str) -> Dict[str, Any]:
        reset_time = datetime.now(pytz.UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        return {
            "api_key": api_key,
            "last_request_time": 0.0,
            "daily_request_count": 0,
            "daily_reset_time": reset_time,
        }

    @staticmethod
    def _is_exhausted_error(message: str) -> bool:
        """Detect provider responses that mean the current key is out of free quota."""
        normalized = message.lower()
        markers = (
            "rate limit",
            "request limit",
            "api credits",
            "quota",
            "limit reached",
            "too many requests",
        )
        return any(marker in normalized for marker in markers)

    def _refresh_key_window(self, state: Dict[str, Any]) -> None:
        now = datetime.now(pytz.UTC)
        if now >= state["daily_reset_time"]:
            state["daily_request_count"] = 0
            state["daily_reset_time"] = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)

    async def _check_rate_limit(self, state: Dict[str, Any]) -> None:
        """Enforce per-key rate limiting between requests (async-safe)."""
        self._refresh_key_window(state)

        if state["daily_request_count"] >= self.max_requests_per_day:
            raise Exception(
                f"Daily API limit reached for current key ({self.max_requests_per_day} requests). "
                f"Resets at {state['daily_reset_time'].strftime('%H:%M UTC')}"
            )

        loop = asyncio.get_event_loop()
        time_since_last = loop.time() - state["last_request_time"]
        if time_since_last < self.request_delay:
            await asyncio.sleep(self.request_delay - time_since_last)

    def _select_available_key(self, start_index: Optional[int] = None) -> Tuple[int, Dict[str, Any]]:
        """Find the next key that still has local request budget remaining."""
        if not self.key_states:
            raise Exception("Twelve Data client disabled (missing API key)")

        base_index = self.current_key_index if start_index is None else start_index
        for offset in range(len(self.key_states)):
            index = (base_index + offset) % len(self.key_states)
            state = self.key_states[index]
            self._refresh_key_window(state)
            if state["daily_request_count"] < self.max_requests_per_day:
                return index, state

        next_reset = min(state["daily_reset_time"] for state in self.key_states)
        raise Exception(
            f"Daily API limit reached for all configured Twelve Data keys "
            f"({len(self.key_states)} keys x {self.max_requests_per_day} requests). "
            f"Next reset at {next_reset.strftime('%H:%M UTC')}"
        )

    def _mark_key_exhausted(self, index: int) -> None:
        """Mark a key as depleted so the next request rotates away from it."""
        state = self.key_states[index]
        self._refresh_key_window(state)
        state["daily_request_count"] = self.max_requests_per_day

    async def _perform_http_get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the underlying HTTP request."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def _make_request(self, endpoint: str, params: Dict[str, Any]) -> Dict:
        """
        Make async API request with error handling and key rotation.

        Args:
            endpoint: API endpoint path
            params: Query parameters

        Returns:
            JSON response data
        """
        if not self.enabled:
            raise Exception("Twelve Data client disabled (missing API key)")

        url = f"{self.BASE_URL}/{endpoint}"
        attempts = 0
        last_error: Optional[Exception] = None

        while attempts < len(self.key_states):
            index, state = self._select_available_key(self.current_key_index)
            await self._check_rate_limit(state)

            request_params = dict(params)
            request_params["apikey"] = state["api_key"]

            try:
                data = await self._perform_http_get(url, request_params)

                loop = asyncio.get_event_loop()
                state["last_request_time"] = loop.time()
                state["daily_request_count"] += 1

                # Check for API-level error messages
                if "status" in data and data["status"] == "error":
                    message = data.get("message", "Unknown error")
                    if self._is_exhausted_error(message):
                        self._mark_key_exhausted(index)
                        self.current_key_index = (index + 1) % len(self.key_states)
                        attempts += 1
                        last_error = Exception(f"API Error: {message}")
                        continue
                    raise Exception(f"API Error: {message}")

                if "code" in data and data["code"] >= 400:
                    message = data.get("message", "Unknown error")
                    if self._is_exhausted_error(message):
                        self._mark_key_exhausted(index)
                        self.current_key_index = (index + 1) % len(self.key_states)
                        attempts += 1
                        last_error = Exception(f"API Error {data['code']}: {message}")
                        continue
                    raise Exception(f"API Error {data['code']}: {message}")

                # Round-robin successful requests so free-tier usage is shared.
                self.current_key_index = (index + 1) % len(self.key_states)
                return data

            except httpx.HTTPStatusError as e:
                if e.response.status_code in {403, 429}:
                    self._mark_key_exhausted(index)
                    self.current_key_index = (index + 1) % len(self.key_states)
                    attempts += 1
                    last_error = Exception(f"HTTP error {e.response.status_code}: {e.response.text}")
                    continue
                raise Exception(f"HTTP error {e.response.status_code}: {e.response.text}")
            except httpx.RequestError as e:
                raise Exception(f"Request failed: {str(e)}")

        if last_error is not None:
            raise last_error
        raise Exception("All configured Twelve Data keys are unavailable.")

    async def get_quote(self, pair: str) -> Dict[str, Any]:
        """
        Get real-time quote for a currency pair.

        Args:
            pair: Currency pair (e.g., "EUR/USD" or "EURUSD")

        Returns:
            Dict with current price, timestamp, etc.
        """
        from utils.formatters import display_pair_format
        normalized = display_pair_format(pair)
        params = {"symbol": normalized, "format": "JSON"}
        return await self._make_request("quote", params)

    async def get_time_series(
        self,
        pair: str,
        interval: str = "5min",
        outputsize: int = 300,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Get historical time series data.

        Args:
            pair: Currency pair
            interval: Time interval (1min, 5min, 15min, 30min, 1h, 1day)
            outputsize: Number of data points (max 5000)
            start_date: Start date (YYYY-MM-DD format)
            end_date: End date (YYYY-MM-DD format)

        Returns:
            DataFrame with columns: datetime, open, high, low, close, volume
        """
        from utils.formatters import display_pair_format
        normalized = display_pair_format(pair)

        params: Dict[str, Any] = {
            "symbol": normalized,
            "interval": interval,
            "outputsize": min(outputsize, 5000),
            "format": "JSON",
            "timezone": "UTC"
        }

        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        data = await self._make_request("time_series", params)

        if "values" not in data:
            raise Exception(f"No data returned for {pair}")

        df = pd.DataFrame(data["values"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)

        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)

        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        else:
            df["volume"] = 0

        df.sort_index(inplace=True)
        return df

    async def get_intraday_data(
        self,
        pair: str,
        interval: str = "5min",
        outputsize: int = 300
    ) -> pd.DataFrame:
        """Get recent intraday data."""
        return await self.get_time_series(pair, interval, outputsize)

    async def get_daily_data(
        self,
        pair: str,
        outputsize: int = 60
    ) -> pd.DataFrame:
        """Get daily historical data."""
        return await self.get_time_series(pair, interval="1day", outputsize=outputsize)

    async def get_historical_sessions(
        self,
        pair: str,
        days_back: int = 60,
        interval: str = "5min"
    ) -> pd.DataFrame:
        """
        Get multiple days of historical intraday data for pattern matching.

        Args:
            pair: Currency pair
            days_back: Number of days to retrieve
            interval: Data interval

        Returns:
            DataFrame with historical intraday data
        """
        interval_minutes = self._parse_interval_minutes(interval)
        candles_per_day = (24 * 60) / interval_minutes
        total_candles = int(days_back * candles_per_day)
        outputsize = min(total_candles, 5000)
        return await self.get_time_series(pair, interval, outputsize)

    @staticmethod
    def _parse_interval_minutes(interval: str) -> int:
        """Parse interval string to minutes."""
        if interval.endswith("min"):
            return int(interval.replace("min", ""))
        if interval.endswith("h"):
            return int(interval.replace("h", "")) * 60
        if interval == "1day":
            return 24 * 60
        raise ValueError(f"Unsupported interval: {interval}")

    def get_rate_limit_status(self) -> Dict[str, Any]:
        """Get aggregate rate limit status across all configured keys."""
        for state in self.key_states:
            self._refresh_key_window(state)

        total_requests = sum(state["daily_request_count"] for state in self.key_states)
        total_limit = len(self.key_states) * self.max_requests_per_day
        next_reset = min(
            (state["daily_reset_time"] for state in self.key_states),
            default=None
        )

        return {
            "requests_today": total_requests,
            "daily_limit": total_limit,
            "remaining": max(total_limit - total_requests, 0),
            "resets_at": next_reset.isoformat() if next_reset else None,
            "percentage_used": round((total_requests / total_limit) * 100, 1) if total_limit else 0,
            "enabled": self.enabled,
            "configured_keys": len(self.key_states),
            "active_key_index": self.current_key_index if self.key_states else None,
            "per_key": [
                {
                    "key_index": index,
                    "requests_today": state["daily_request_count"],
                    "daily_limit": self.max_requests_per_day,
                    "remaining": max(self.max_requests_per_day - state["daily_request_count"], 0),
                    "resets_at": state["daily_reset_time"].isoformat(),
                }
                for index, state in enumerate(self.key_states)
            ],
        }


class NullDataClient:
    """Fallback client when no API key is available."""

    enabled = False

    async def get_intraday_data(self, *args, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    async def get_time_series(self, *args, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    async def get_daily_data(self, *args, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    async def get_historical_sessions(self, *args, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    def get_rate_limit_status(self) -> Dict[str, Any]:
        return {
            "requests_today": 0,
            "daily_limit": 0,
            "remaining": 0,
            "resets_at": None,
            "percentage_used": 0,
            "enabled": False
        }


# Singleton instance
_client_instance: Optional[TwelveDataClient] = None


def get_client() -> TwelveDataClient:
    """Get singleton Twelve Data client instance."""
    global _client_instance
    if _client_instance is None:
        try:
            _client_instance = TwelveDataClient()
            if not _client_instance.enabled:
                _client_instance = NullDataClient()
        except Exception:
            _client_instance = NullDataClient()
    return _client_instance
