#========================================================================================================================
# File: market_monitor.py
#========================================================================================================================
import asyncio
import os
import time
import joblib
import pandas as pd
import numpy as np

from typing import Any, Dict, List, Optional, Callable, Union
from sklearn.linear_model import LinearRegression
from cachetools import TTLCache
from web3 import AsyncWeb3

from api_config import API_Config
from configuration import Configuration
import logging as logger

logger = logger.getLogger(__name__)

class Market_Monitor:
    """Advanced market monitoring system for real-time analysis and prediction."""
    """
    Monitors market conditions, including price movements, liquidity, and volatility.
    Provides real-time data for decision-making in MEV strategies.
    """
    # Class Constants
    VOLATILITY_THRESHOLD: float = 0.05  # 5% standard deviation
    LIQUIDITY_THRESHOLD: int = 100_000  # $100,000 in 24h volume
    PRICE_EMA_SHORT_PERIOD: int = 12
    PRICE_EMA_LONG_PERIOD: int = 26


    def __init__(
        self,
        web3: "AsyncWeb3",
        configuration: Optional["Configuration"],
        api_config: Optional["API_Config"],
        transaction_core: Optional[Any] = None,
    ) -> None:
        """Initialize Market Monitor with required components."""
        """
        Initialize the Market Monitor.
        Args:
            web3: An AsyncWeb3 instance.
            configuration: Configuration object containing settings.
            api_config: API configuration instance.
            transaction_core: Transaction Core instance (optional).
        """        
        self.web3: "AsyncWeb3" = web3
        self.configuration: Optional["Configuration"] = configuration
        self.api_config: Optional["API_Config"] = api_config
        self.transaction_core: Optional[Any] = transaction_core  # Store transaction_core reference
        self.price_model: Optional[LinearRegression] = LinearRegression()
        self.model_last_updated: float = 0
        self.session: Optional[aiohttp.ClientSession] = None  # Managed session
        self.price_cache: TTLCache = TTLCache(maxsize=1000, ttl=self.configuration.MARKET_MONITOR_CACHE_TTL)
        self.volatility_cache: TTLCache = TTLCache(maxsize=100, ttl=self.configuration.VOLATILITY_CACHE_TTL)

        # Get from config or default
        self.linear_regression_path: str = self.configuration.get_config_value("LINEAR_REGRESSION_PATH")
        self.model_path: str = self.configuration.get_config_value("MODEL_PATH")
        self.training_data_path: str = self.configuration.get_config_value("TRAINING_DATA_PATH")

        # Create directory if it doesn't exist
        os.makedirs(self.linear_regression_path, exist_ok=True)

        # Separate caches for different data types
        self.caches: Dict[str, TTLCache] = {
            'price': TTLCache(maxsize=2000,
                    ttl=300),
            'volume': TTLCache(maxsize=1000,
                     ttl=900),
            'volatility': TTLCache(maxsize=200,
                     ttl=600)
        }

        # Initialize model variables
        self.price_model: Optional[LinearRegression] = None
        self.last_training_time: float = 0
        self.model_accuracy: float = 0.0
        self.RETRAINING_INTERVAL: int = self.configuration.MODEL_RETRAINING_INTERVAL if self.configuration else 3600 # Retrain every hour
        self.MIN_TRAINING_SAMPLES: int = self.configuration.MIN_TRAINING_SAMPLES if self.configuration else 100

        # Initialize data storage
        self.historical_data: pd.DataFrame = pd.DataFrame()
        self.prediction_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)  # 5-minute cache

        # Data update
        self.update_scheduler: Dict[str, int] = {
            'training_data': 0,  # Last update timestamp
            'model': 0,          # Last model update timestamp
            'model_retraining_interval': self.configuration.MODEL_RETRAINING_INTERVAL if self.configuration else 3600,  

        }

    async def __aenter__(self) -> "Market_Monitor":
        """Context manager entry point."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit point."""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.debug("MarketMonitor session closed.")

    async def initialize(self) -> None:
        """Initialize market monitor components and load model."""
        try:
            self.price_model = LinearRegression()
            self.model_last_updated = 0
            # Initialize caches
            self.price_cache = TTLCache(maxsize=1000, ttl=self.configuration.MARKET_MONITOR_CACHE_TTL)
            self.volatility_cache = TTLCache(maxsize=100, ttl=self.configuration.VOLATILITY_CACHE_TTL)
          
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)

            # Load existing model if available, or create new one
            model_loaded = False
            if os.path.exists(self.model_path):
                try:
                    self.price_model = joblib.load(self.model_path)
                    logger.debug("Loaded existing price prediction model")
                    model_loaded = True
                except (OSError, KeyError) as e:
                    logger.warning(f"Failed to load model: {e}. Creating new model.")
                    self.price_model = LinearRegression()

            if not model_loaded:
                logger.debug("Creating new price prediction model")
                self.price_model = LinearRegression()
                # Save initial model
                try:
                    joblib.dump(self.price_model, self.model_path)
                    logger.debug("Saved initial price prediction model")
                except Exception as e:
                    logger.warning(f"Failed to save initial model: {e}")

            # Load or create training data file
            if os.path.exists(self.training_data_path):
                try:
                    self.historical_data = pd.read_csv(self.training_data_path)
                    logger.debug(f"Loaded {len(self.historical_data)} historical data points")
                except Exception as e:
                    logger.warning(f"Failed to load historical data: {e}. Starting with empty dataset.")
                    self.historical_data = pd.DataFrame()
            else:
                self.historical_data = pd.DataFrame()

            # Initial model training if needed
            if len(self.historical_data) >= self.MIN_TRAINING_SAMPLES:
                await self._train_model()
            # Verify web3 connection
            if not self.web3:
                raise RuntimeError("Web3 not initialized in Market_Monitor")
            if not await self.web3.is_connected():
                raise RuntimeError("Web3 connection failed in Market_Monitor")
            logger.debug("Market Monitor initialized ✅")

            # Start update scheduler
            asyncio.create_task(self.schedule_updates())

        except Exception as e:
            logger.critical(f"Market Monitor initialization failed: {e}")
            raise RuntimeError(f"Market Monitor initialization failed: {e}")

    async def schedule_updates(self) -> None:
        """Schedule periodic data and model updates."""
        while True:
            try:
                current_time = time.time()

                # Update training data
                if current_time - self.update_scheduler['training_data'] >= self.update_scheduler['model_retraining_interval']: # Corrected to use model_retraining_interval
                    await self.api_config.update_training_data()
                    self.update_scheduler['training_data'] = current_time

                # Retrain model
                if current_time - self.update_scheduler['model'] >= self.update_scheduler['model_retraining_interval']: # Corrected to use model_retraining_interval
                    await self._train_model()
                    self.update_scheduler['model'] = current_time

                await asyncio.sleep(60)  # Check every minute

            except Exception as e:
                logger.error(f"Error in update scheduler: {e}")
                await asyncio.sleep(300)  # Wait 5 minutes on error

    async def check_market_conditions(self, token_address: str) -> Dict[str, bool]:
        """Analyze current market conditions for a given token."""
        market_conditions = {
            "high_volatility": False,
            "bullish_trend": False,
            "bearish_trend": False,
            "low_liquidity": False,
        }

        # Get symbol from address using API_Config's method
        symbol = self.api_config.get_token_symbol(token_address)
        if not symbol:
            logger.debug(f"Cannot get token symbol for address {token_address}")
            return market_conditions

        try:
            # Get the API-specific ID for the token
            api_symbol = self.api_config._normalize_symbol(symbol)

            # Use API-specific symbol for all API calls
            prices = await self.api_config.get_token_price_data(api_symbol, 'historical', timeframe=1)
            if len(prices) < 2:
                logger.debug(f"Not enough price data to analyze market conditions for {symbol}")
                return market_conditions

            volatility = await self.api_config._calculate_volatility(prices)
            if volatility > self.VOLATILITY_THRESHOLD:
                market_conditions["high_volatility"] = True
            logger.debug(f"Calculated volatility for {symbol}: {volatility}")

            moving_average = np.mean(prices)
            if prices[-1] > moving_average:
                market_conditions["bullish_trend"] = True
            elif prices[-1] < moving_average:
                market_conditions["bearish_trend"] = True

            volume = await self.get_token_volume(api_symbol)
            if volume < self.LIQUIDITY_THRESHOLD:
                market_conditions["low_liquidity"] = True

        except Exception as e:
            logger.error(f"Error checking market conditions for {symbol}: {e}")

        return market_conditions

    # Price Analysis Methods
    async def predict_price_movement(self, token_symbol: str) -> float:
        """Predict future price movement."""
        try:
            cache_key = f"prediction_{token_symbol}"
            if cache_key in self.prediction_cache:
                return self.prediction_cache[cache_key]

            # Use API_Config's prediction method
            prediction = await self.api_config.predict_price(token_symbol)
            self.prediction_cache[cache_key] = prediction
            return prediction

        except Exception as e:
            logger.error(f"Error predicting price movement: {e}")
            return 0.0

    async def _get_market_features(self, token_symbol: str) -> Optional[Dict[str, float]]:
        """Get current market features for prediction with  metrics."""
        try:
            # Use API-specific symbol format
            api_symbol = self.api_config._normalize_symbol(token_symbol)

            # Gather data concurrently using API-specific symbol
            price, volume, supply_data, market_data, prices = await asyncio.gather(
                self.api_config.get_real_time_price(api_symbol),
                self.api_config.get_token_volume(api_symbol),
                self.api_config.get_token_supply_data(api_symbol),
                self._get_trading_metrics(api_symbol),
                self.get_token_price(api_symbol, data_type='historical', timeframe=1),
                return_exceptions=True
            )

            if any(isinstance(r, Exception) for r in [price, volume, supply_data, market_data, prices]):
                logger.warning(f"Error fetching market data for {token_symbol}")
                return None

            # Basic features
            features = {
                'market_cap': await self.api_config.get_token_market_cap(token_symbol),
                'volume_24h': float(volume),
                'percent_change_24h': await self.api_config.get_price_change_24h(token_symbol),
                'total_supply': supply_data.get('total_supply', 0),
                'circulating_supply': supply_data.get('circulating_supply', 0),
                 'volatility': await self.api_config._calculate_volatility(prices) if prices else 0,
                'price_momentum': await self.api_config._calculate_momentum(prices) if prices else 0,
                'liquidity_ratio': await self._calculate_liquidity_ratio(token_symbol),
                **market_data
            }

            return features

        except Exception as e:
            logger.error(f"Error fetching market features: {e}")
            return None

    async def _get_trading_metrics(self, token_symbol: str) -> Dict[str, float]:
        """Get additional trading metrics."""
        try:
            return {
                'avg_transaction_value': await self._get_avg_transaction_value(token_symbol),
                'trading_pairs': await self._get_trading_pairs_count(token_symbol),
                'exchange_count': await self._get_exchange_count(token_symbol),
                'buy_sell_ratio': await self._get_buy_sell_ratio(token_symbol),
                'smart_money_flow': await self._get_smart_money_flow(token_symbol)
            }
        except Exception as e:
            logger.error(f"Error getting trading metrics: {e}")
            return {
                'avg_transaction_value': 0.0,
                'trading_pairs': 0.0,
                'exchange_count': 0.0,
                'buy_sell_ratio': 1.0,
                'smart_money_flow': 0.0
            }

    # Calculate new metrics
    async def _get_avg_transaction_value(self, token_symbol: str) -> float:
        """Get average transaction value over last 24h."""
        try:
            volume = await self.api_config.get_token_volume(token_symbol)
            tx_count = await self._get_transaction_count(token_symbol)
            return volume / tx_count if tx_count > 0 else 0.0
        except Exception as e:
            logger.error(f"Error calculating avg transaction value: {e}")
            return 0.0

    # Helper methods to calculate new metrics
    async def _get_transaction_count(self, token_symbol: str) -> int:
        """Get number of transactions in 24 hrs using api config."""
        try:
            # This data is not available from the api config therefore, this will return 0.
            return 0
        except Exception as e:
             logger.error(f"Error getting transaction count {e}")
             return 0

    async def _get_trading_pairs_count(self, token_symbol: str) -> int:
        """Get number of trading pairs for a token using api config."""
        try:
            metadata = await self.api_config.get_token_metadata(token_symbol)
            return len(metadata.get('trading_pairs', [])) if metadata else 0
        except Exception as e:
            logger.error(f"Error getting trading pairs for {token_symbol}: {e}")
            return 0

    async def _get_exchange_count(self, token_symbol: str) -> int:
        """Get number of exchanges the token is listed on using api config."""
        try:
           metadata = await self.api_config.get_token_metadata(token_symbol)
           return len(metadata.get('exchanges', [])) if metadata else 0
        except Exception as e:
           logger.error(f"Error getting exchange count for {token_symbol}: {e}")
           return 0


    async def _get_buy_sell_ratio(self, token_symbol: str) -> float:
        """
        Calculate buy/sell ratio using available API data.
        Returns ratio > 1 for more buys, < 1 for more sells.
        """
        try:
            # Try Binance API first as it has best real-time data
            config = self.api_config.api_configs.get('binance')
            if config:
                # Get trading pairs for the token
                pairs = await self._get_trading_pairs(token_symbol)
                if pairs:
                    total_buys = 0
                    total_sells = 0

                    for pair in pairs[:3]:  # Limit to top 3 pairs
                        url = f"{config['base_url']}/ticker/24hr"
                        params = {"symbol": pair}
                        data = await self.api_config.make_request('binance', url, params=params)

                        if data:
                            # Use count of upward vs downward trades
                            buys = float(data.get('count', 0)) * (float(data.get('priceChangePercent', 0)) > 0)
                            sells = float(data.get('count', 0)) * (float(data.get('priceChangePercent', 0)) < 0)
                            total_buys += buys
                            total_sells += sells

                    if total_sells > 0:
                        return total_buys / total_sells

            # Fallback to CoinGecko's sentiment data
            config = self.api_config.api_configs.get('coingecko')
            if config:
                url = f"{config['base_url']}/coins/{token_symbol.lower()}"

                # Updated header logic
                api_key = self.api_config._get_current_coingecko_api_key()
                if api_key:
                    if self.configuration.COINGECKO_API_KEY_TYPE == "paid":
                        headers = {"x-cg-pro-api-key": api_key}
                    elif self.configuration.COINGECKO_API_KEY_TYPE == "free":
                        headers = {"x-cg-demo-api-key": api_key}
                else:
                    headers = None

                data = await self.api_config.make_request('coingecko', url, headers=headers)

                if data and 'sentiment_votes_up_percentage' in data:
                    up_votes = data['sentiment_votes_up_percentage']
                    down_votes = data['sentiment_votes_down_percentage']
                    if down_votes > 0:
                        return up_votes / down_votes

            return 1.0  # Neutral ratio if no data available

        except Exception as e:
            logger.error(f"Error calculating buy/sell ratio: {e}")
            return 1.0

    async def _get_smart_money_flow(self, token_symbol: str) -> float:
        """
        Calculate smart money flow indicator using wallet analysis and volume.
        Returns a value between -1 and 1 where:
        - Positive values indicate smart money flowing in
        - Negative values indicate smart money flowing out
        """
        try:
            smart_money_score = 0.0
            indicators = 0

            # Get volume data from CoinGecko
            config = self.api_config.api_configs.get('coingecko')
            if config:
                url = f"{config['base_url']}/coins/{token_symbol.lower()}/market_chart"
                params = {"vs_currency": "usd", "days": "1", "interval": "hourly"}
                
                # Line 386: Same updated logic for consistency
                api_key = self.api_config._get_current_coingecko_api_key()
                if api_key:
                    if self.configuration.COINGECKO_API_KEY_TYPE == "paid":
                        headers = {"x-cg-pro-api-key": api_key}
                    elif self.configuration.COINGECKO_API_KEY_TYPE == "free":
                        headers = {"x-cg-demo-api-key": api_key}
                else:
                    headers = None

                data = await self.api_config.make_request('coingecko', url, params=params, headers=headers)
                if data and 'volumes' in data:
                    # Analyze volume trend
                    volumes = [float(v[1]) for v in data['volumes']]
                    if len(volumes) > 12:  # Need at least 12 hours of data
                        recent_vol = sum(volumes[-6:])  # Last 6 hours
                        prev_vol = sum(volumes[-12:-6])  # Pous 6 hours
                        if prev_vol > 0:
                            vol_change = (recent_vol - prev_vol) / prev_vol
                            smart_money_score += vol_change
                            indicators += 1

            # Get market data from CoinMarketCap
            config = self.api_config.api_configs.get('coinmarketcap')
            if config:
                url = f"{config['base_url']}/cryptocurrency/quotes/latest"
                params = {"symbol": token_symbol.upper()}
                headers = {"X-CMC_PRO_API_KEY": config['api_key']}

                data = await self.api_config.make_request('coinmarketcap', url, params=params, headers=headers)
                if data and 'data' in data:
                    token_data = data['data'].get(token_symbol.upper(), {})
                    quote = token_data.get('quote', {}).get('USD', {})

                    # Analyze market cap vs volume ratio
                    market_cap = quote.get('market_cap', 0)
                    volume_24h = quote.get('volume_24h', 0)

                    if market_cap > 0:
                        volume_ratio = volume_24h / market_cap
                        if volume_ratio > 0.1:  # High volume relative to market cap
                            smart_money_score += 0.5
                        elif volume_ratio < 0.01:  # Low volume relative to market cap
                            smart_money_score -= 0.5
                        indicators += 1

            # Get price data from CryptoCompare
            config = self.api_config.api_configs.get('cryptocompare')
            if config:
                url = f"{config['base_url']}/v2/histohour"
                params = {
                    "fsym": token_symbol.upper(),
                    "tsym": "USD",
                    "limit": 24,
                    "api_key": config['api_key']
                }

                data = await self.api_config.make_request('cryptocompare', url, params=params)
                if data and 'Data' in data:
                    prices = [float(d['close']) for d in data['Data']]
                    if len(prices) > 12:
                        # Analyze price trend
                        recent_avg = sum(prices[-6:]) / 6
                        prev_avg = sum(prices[-12:-6]) / 6
                        if prev_avg > 0:
                            price_change = (recent_avg - prev_avg) / prev_avg
                            smart_money_score += price_change
                            indicators += 1

            # Normalize the score between -1 and 1
            if indicators > 0:
                final_score = smart_money_score / indicators
                return max(min(final_score, 1.0), -1.0)

            return 0.0  # Neutral score if no data available

        except Exception as e:
            logger.error(f"Error calculating smart money flow: {e}")
            return 0.0


    async def update_training_data(self, new_data: Dict[str, Any]) -> None:
        """Update training data with new market information."""
        try:
            # Convert new data to DataFrame row
            df_row = pd.DataFrame([new_data])

            # Append to historical data
            self.historical_data = pd.concat([self.historical_data, df_row], ignore_index=True)

            # Save updated data
            self.historical_data.to_csv(self.training_data_path, index=False)

            # Retrain model if enough new data
            if len(self.historical_data) >= self.MIN_TRAINING_SAMPLES:
                await self._train_model()

        except Exception as e:
            logger.error(f"Error updating training data: {e}")

    async def _train_model(self) -> None:
        """ model training with feature importance analysis."""
        try:
            if len(self.historical_data) < self.MIN_TRAINING_SAMPLES:
                logger.warning("Insufficient data for model training")
                return

            # Define all features we want to use
            features = [
                'market_cap', 'volume_24h', 'percent_change_24h',
                'total_supply', 'circulating_supply', 'volatility',
                'liquidity_ratio', 'avg_transaction_value', 'trading_pairs',
                'exchange_count', 'price_momentum', 'buy_sell_ratio',
                'smart_money_flow'
            ]

            X = self.historical_data[features]
            y = self.historical_data['price_usd']

            # Train/test split with shuffling
            train_size = int(len(X) * 0.8)
            indices = np.random.permutation(len(X))
            X_train = X.iloc[indices[:train_size]]
            X_test = X.iloc[indices[train_size:]]
            y_train = y.iloc[indices[:train_size]]
            y_test = y.iloc[indices[train_size:]]

            # Train model
            self.price_model = LinearRegression()
            self.price_model.fit(X_train, y_train)

            # Calculate and log feature importance
            importance = pd.DataFrame({
                'feature': features,
                'importance': np.abs(self.price_model.coef_)
            })
            importance = importance.sort_values('importance', ascending=False)
            logger.debug("Feature importance:\n" + str(importance))

            # Calculate accuracy
            self.model_accuracy = self.price_model.score(X_test, y_test)

            # Save model and accuracy metrics
            joblib.dump(self.price_model, self.model_path)

            self.last_training_time = time.time()
            logger.debug(f"Model trained successfully. Accuracy: {self.model_accuracy:.4f}")

        except Exception as e:
            logger.error(f"Error training model: {e}")

    # Market Data Methods
    async def get_price_data(self, *args, **kwargs):
        """Use centralized price fetching from API_Config."""
        return await self.api_config.get_token_price_data(*args, **kwargs)

    async def get_token_volume(self, token_symbol: str) -> float:
        """
        Get the 24-hour trading volume for a given token symbol.

        :param token_symbol: Token symbol to fetch volume for
        :return: 24-hour trading volume
        """
        cache_key = f"token_volume_{token_symbol}"
        if cache_key in self.caches['volume']:
            logger.debug(f"Returning cached trading volume for {token_symbol}.")
            return self.caches['volume'][cache_key]

        volume = await self.api_config._fetch_from_services( 
            lambda _: self.api_config.get_token_volume(token_symbol),
            f"trading volume for {token_symbol}"
        )
        if volume is not None:
            self.caches['volume'][cache_key] = volume
        return volume or 0.0


    async def stop(self) -> None:
        """Clean up resources and stop monitoring."""
        try:
            # Clear caches and clean up resources
            for cache in self.caches.values():
                cache.clear()
            if self.session and not self.session.closed:
                await self.session.close()                
            logger.debug("Market Monitor stopped.")
        except Exception as e:
            logger.error(f"Error stopping Market Monitor: {e}")

    async def get_token_price(self, token_symbol: str, data_type: str = 'current', timeframe: int = 1, vs_currency: str = 'eth') -> Union[float, List[float]]:
        """Delegate to API_Config for price data."""
        return await self.api_config.get_token_price_data(token_symbol, data_type, timeframe, vs_currency)
