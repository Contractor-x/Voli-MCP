"""
Unified price feed with Twelve Data and Alpha Vantage fallback.
"""

from typing import Optional
import pandas as pd

from data.twelve_data_client import TwelveDataClient
from data.alpha_vantage_client import AlphaVantageClient


class PriceFeed:
    """Facade over multiple data providers with fallback."""

    def __init__(self):
        self.twelve: Optional[TwelveDataClient] = None
        self.alpha: Optional[AlphaVantageClient] = None

        try:
            twelve_client = TwelveDataClient()
            if twelve_client.enabled:
                self.twelve = twelve_client
        except Exception:
            self.twelve = None

        try:
            self.alpha = AlphaVantageClient()
        except Exception:
            self.alpha = None

        if self.twelve is None and self.alpha is None:
            raise ValueError(
                "No price feed API keys found "
                "(TWELVE_DATA_API_KEYS, TWELVE_DATA_API_KEY, or ALPHA_VANTAGE_API_KEY)"
            )

    async def get_intraday_data(
        self,
        pair: str,
        interval: str = "5min",
        outputsize: int = 300
    ) -> pd.DataFrame:
        """Get intraday data with provider fallback."""
        if self.twelve is not None:
            try:
                return await self.twelve.get_intraday_data(pair, interval=interval, outputsize=outputsize)
            except Exception:
                pass

        if self.alpha is not None:
            # Alpha Vantage uses compact/full string, so approximate based on outputsize
            outputsize_mode = "compact" if outputsize <= 100 else "full"
            return await self.alpha.get_intraday_data(pair, interval=interval, outputsize=outputsize_mode)

        raise Exception("All price feed providers failed.")


_price_feed_instance: Optional[PriceFeed] = None


def get_price_feed() -> PriceFeed:
    """Get singleton price feed instance."""
    global _price_feed_instance
    if _price_feed_instance is None:
        _price_feed_instance = PriceFeed()
    return _price_feed_instance
