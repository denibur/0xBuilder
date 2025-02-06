#========================================================================================================================
# File: configuration.py
#========================================================================================================================
import os
import json
import aiofiles
import dotenv
import aiohttp # Import aiohttp for API key validation requests

from eth_utils import function_signature_to_4byte_selector
from pathlib import Path
from typing import Any, Dict, List, Optional

import logging as logger

logger = logger.getLogger(__name__)

# Constants
DEFAULT_MAX_GAS_PRICE = 100_000_000_000  # 100 Gwei in wei
DEFAULT_GAS_LIMIT = 1_000_000
DEFAULT_MAX_SLIPPAGE = 0.01
DEFAULT_MIN_PROFIT = 0.001
DEFAULT_MIN_BALANCE = 0.000001

# Standard Ethereum addresses
MAINNET_ADDRESSES = {
    'WETH': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2',
    'USDC': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
    'USDT': '0xdAC17F958D2ee523a2206206994597C13D831ec7'
}

class Configuration:
    """
    Loads configuration from environment variables and JSON files.
    Provides methods to access and validate configuration data.
    """

    def __init__(self, env_path: Optional[str] = None) -> None:
        """
        Initialize Configuration with default values.
        Args:
            env_path: Optional path to the .env file. Defaults to the current directory.
        """
        self.env_path = env_path if env_path else ".env"
        self._load_env()
        self._initialize_defaults()
        self.signatures: Dict[str, Dict[str, str]] = {}
        self.method_selectors: Dict[str, Dict[str, str]] = {}
        self.BASE_PATH = Path(__file__).parent.parent

    def _initialize_defaults(self) -> None:
        """Initialize configuration with default values."""
        # General settings
        self.MAX_GAS_PRICE = self._get_env_int("MAX_GAS_PRICE", DEFAULT_MAX_GAS_PRICE)
        self.GAS_LIMIT = self._get_env_int("GAS_LIMIT", DEFAULT_GAS_LIMIT)
        self.MAX_SLIPPAGE = self._get_env_float("MAX_SLIPPAGE", DEFAULT_MAX_SLIPPAGE)
        self.MIN_PROFIT = self._get_env_float("MIN_PROFIT", DEFAULT_MIN_PROFIT)
        self.MIN_BALANCE = self._get_env_float("MIN_BALANCE", DEFAULT_MIN_BALANCE)

        # Nonce Core settings
        self.NONCE_CACHE_TTL = self._get_env_int("NONCE_CACHE_TTL", 300)  # Default: 5 minutes
        self.NONCE_RETRY_DELAY = self._get_env_int("NONCE_RETRY_DELAY", 2)  # Default: 2 seconds
        self.NONCE_MAX_RETRIES = self._get_env_int("NONCE_MAX_RETRIES", 3)  # Default: 3 retries
        self.NONCE_TRANSACTION_TIMEOUT = self._get_env_int("NONCE_TRANSACTION_TIMEOUT", 60)  # Default: 60 seconds

        self.SAFETY_NET_CACHE_TTL = int(os.getenv("SAFETY_NET_CACHE_TTL", 300))  # Default to 300 if not set
        self.SAFETY_NET_GAS_PRICE_TTL = int(os.getenv("SAFETY_NET_GAS_PRICE_TTL", 60))  # Default to 60 if not set

        # Add BASE_PATH to defaults
        self.BASE_PATH = Path(os.getenv("BASE_PATH", str(Path(__file__).parent.parent)))

        # Standard addresses
        self.WETH_ADDRESS = MAINNET_ADDRESSES['WETH']
        self.USDC_ADDRESS = MAINNET_ADDRESSES['USDC']
        self.USDT_ADDRESS = MAINNET_ADDRESSES['USDT']

        # API Keys and Endpoints
        self.ETHERSCAN_API_KEY: str = self._get_env_str("ETHERSCAN_API_KEY")
        self.INFURA_API_KEY: str = self._get_env_str("INFURA_API_KEY")

        # CoinGecko API Key Handling
        self.COINGECKO_API_KEY_TYPE: str = self._get_env_str("COINGECKO_API_KEY_TYPE", "free")
        if self.COINGECKO_API_KEY_TYPE == "paid":
            self.COINGECKO_PAID_API_KEY: str = self._get_env_str("COINGECKO_PAID_API_KEY")
            self.COINGECKO_FREE_API_KEYS: List[str] = []
        elif self.COINGECKO_API_KEY_TYPE == "free":
            coingecko_free_keys_raw: str = self._get_env_str("COINGECKO_FREE_API_KEYS")
            self.COINGECKO_FREE_API_KEYS: List[str] = [
                key.strip() for key in coingecko_free_keys_raw.split(",")
            ]
            # Dynamically assign individual keys as attributes
            for idx, api_key in enumerate(self.COINGECKO_FREE_API_KEYS):
                setattr(self, f"COINGECKO_FREE_API_KEY_{idx}", api_key)
        else:
            raise ValueError(f"Invalid COINGECKO_API_KEY_TYPE: {self.COINGECKO_API_KEY_TYPE}")


        self.COINMARKETCAP_API_KEY: str = self._get_env_str("COINMARKETCAP_API_KEY")
        self.CRYPTOCOMPARE_API_KEY: str = self._get_env_str("CRYPTOCOMPARE_API_KEY")
        self.HTTP_ENDPOINT: Optional[str] = self._get_env_str("HTTP_ENDPOINT", None)
        self.WEBSOCKET_ENDPOINT: Optional[str] = self._get_env_str("WEBSOCKET_ENDPOINT", None)
        self.IPC_ENDPOINT: Optional[str] = self._get_env_str("IPC_ENDPOINT", None)

        # Account
        self.WALLET_ADDRESS: str = self._get_env_str("WALLET_ADDRESS")
        self.WALLET_KEY: str = self._get_env_str("WALLET_KEY")

        # Paths
        self.ERC20_ABI: Path = self._resolve_path("ERC20_ABI")
        self.AAVE_FLASHLOAN_ABI: Path = self._resolve_path("AAVE_FLASHLOAN_ABI")
        self.AAVE_POOL_ABI: Path = self._resolve_path("AAVE_POOL_ABI")
        self.UNISWAP_ABI: Path = self._resolve_path("UNISWAP_ABI")
        self.SUSHISWAP_ABI: Path = self._resolve_path("SUSHISWAP_ABI")
        self.ERC20_SIGNATURES: Path = self._resolve_path("ERC20_SIGNATURES")
        self.TOKEN_ADDRESSES: Path = self._resolve_path("TOKEN_ADDRESSES")
        self.TOKEN_SYMBOLS: Path = self._resolve_path("TOKEN_SYMBOLS")

        # Router Addresses
        self.UNISWAP_ADDRESS: str = self._get_env_str("UNISWAP_ADDRESS")
        self.SUSHISWAP_ADDRESS: str = self._get_env_str("SUSHISWAP_ADDRESS")
        self.AAVE_POOL_ADDRESS: str = self._get_env_str("AAVE_POOL_ADDRESS")
        self.AAVE_FLASHLOAN_ADDRESS: str = self._get_env_str("AAVE_FLASHLOAN_ADDRESS")

        # Set default values for strategies
        self.SLIPPAGE_DEFAULT: float = 0.1
        self.SLIPPAGE_MIN: float = 0.01
        self.SLIPPAGE_MAX: float = 0.5
        self.SLIPPAGE_HIGH_CONGESTION: float = 0.05
        self.SLIPPAGE_LOW_CONGESTION: float = 0.2
        self.MAX_GAS_PRICE_GWEI: int = 500
        self.MIN_PROFIT_MULTIPLIER: float = 2.0
        self.BASE_GAS_LIMIT: int = 21000

        # ML model configuration
        self.MODEL_RETRAINING_INTERVAL: int = 3600  # 1 hour
        self.MIN_TRAINING_SAMPLES: int = 100
        self.MODEL_ACCURACY_THRESHOLD: float = 0.7
        self.PREDICTION_CACHE_TTL: int = 300  # 5 minutes
        self.LINEAR_REGRESSION_PATH: str = str(self.BASE_PATH / "linear_regression")
        self.MODEL_PATH: str = str(self.BASE_PATH / "linear_regression" / "price_model.joblib")
        self.TRAINING_DATA_PATH: str = str(self.BASE_PATH / "linear_regression" / "training_data.csv")

        # Ensure ML paths are absolute
        self.LINEAR_REGRESSION_PATH = str(self.BASE_PATH / "linear_regression")
        self.MODEL_PATH = str(self.BASE_PATH / "linear_regression" / "price_model.joblib")
        self.TRAINING_DATA_PATH = str(self.BASE_PATH / "linear_regression" / "training_data.csv")

        # Create directories if they don't exist
        os.makedirs(self.LINEAR_REGRESSION_PATH, exist_ok=True)

        # Configurable Time Intervals
        self.MEMORY_CHECK_INTERVAL = self._get_env_int("MEMORY_CHECK_INTERVAL", 300)
        self.COMPONENT_HEALTH_CHECK_INTERVAL = self._get_env_int("COMPONENT_HEALTH_CHECK_INTERVAL", 60)
        self.PROFITABLE_TX_PROCESS_TIMEOUT = self._get_env_float("PROFITABLE_TX_PROCESS_TIMEOUT", 1.0)


    def _load_env(self) -> None:
        """Load environment variables fromenv file."""
        dotenv.load_dotenv(dotenv_path=self.env_path)
        logger.debug(f"Environment variables loaded from: {self.env_path}")

    def _validate_ethereum_address(self, address: str, var_name: str) -> str:
        """
        Validate Ethereum address format with improved checks.
        Returns: Normalized address string
        Raises: ValueError if invalid
        """
        if not isinstance(address, str):
            raise ValueError(f"{var_name} must be a string, got {type(address)}")

        clean_address = address.lower().strip()
        if not clean_address.startswith('0x'):
            clean_address = '0x' + clean_address

        if len(clean_address) != 42:
            raise ValueError(f"Invalid {var_name} length: {len(clean_address)}")

        try:
            int(clean_address[2:], 16)
            return clean_address
        except ValueError:
            raise ValueError(f"Invalid hex format for {var_name}")

    def _get_env_str(self, var_name: str, default: Optional[str] = None) -> str:
        """Get an environment variable as string, raising error if missing."""
        value = os.getenv(var_name, default)
        if value is None:
            logger.error(f"Missing environment variable: {var_name}")
            raise ValueError(f"Missing environment variable: {var_name}")

        # Only validate Ethereum addresses for contract addresses
        if 'ADDRESS' in var_name and not any(path_suffix in var_name for path_suffix in ['ABI', 'PATH', 'ADDRESSES', 'SIGNATURES', 'SYMBOLS']):
            return self._validate_ethereum_address(value, var_name)

        return value

    def _parse_numeric_string(self, value: str) -> str:
        """Remove underscores and comments from numeric strings."""
        value = value.split('#')[0].strip()
        return value.replace('_', '')

    def _get_env_int(self, var_name: str, default: Optional[int] = None) -> int:
        """Get an environment variable as int, raising error if missing or invalid."""
        try:
            value = os.getenv(var_name)
            if value is None:
                if default is None:
                    raise ValueError(f"Missing environment variable: {var_name}")
                return default
            # Clean the value before parsing
            cleaned_value = self._parse_numeric_string(value)
            return int(cleaned_value)
        except ValueError as e:
            logger.error(f"Invalid or missing integer environment variable: {var_name} - {e}")
            raise

    def _get_env_float(self, var_name: str, default: Optional[float] = None) -> float:
        """Get an environment variable as float, raising error if missing or invalid."""
        try:
            value = os.getenv(var_name)
            if value is None:
                if default is None:
                    raise ValueError(f"Missing environment variable: {var_name}")
                return default
            # Clean the value before parsing
            cleaned_value = self._parse_numeric_string(value)
            return float(cleaned_value)
        except ValueError as e:
            logger.error(f"Invalid or missing float environment variable: {var_name} - {e}")
            raise

    def _resolve_path(self, path_env_var: str) -> Path:
        """Resolve a path from environment variable and ensure the file exists."""
        path_str = self._get_env_str(path_env_var)
        full_path = self.BASE_PATH / path_str
        if not full_path.exists():
            logger.error(f"File not found: {full_path}")
            raise FileNotFoundError(f"File not found: {full_path}")
        logger.debug(f"Resolved path: {full_path}")
        return full_path

    async def _load_json_safe(self, file_path: Path, description: str) -> Any:
        """Load JSON with better error handling and validation."""
        try:
            async with aiofiles.open(file_path, 'r') as f:
                content = await f.read()
                try:
                    data = json.loads(content)
                    logger.debug(f"Loaded {description} from {file_path}")
                    return data
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON in {description}: {e}")
        except FileNotFoundError:
            raise FileNotFoundError(f"Missing {description} file: {file_path}")
        except Exception as e:
            raise RuntimeError(f"Error loading {description}: {e}")

    async def get_token_addresses(self) -> List[str]:
         """Get the list of monitored token addresses from the config file."""
         data = await self._load_json_safe(self.TOKEN_ADDRESSES, "monitored tokens")
         if not isinstance(data, dict):
             logger.error("Invalid format for token addresses: must be a dictionary")
             raise ValueError("Invalid format for token addresses: must be a dictionary")

         # Extract addresses from dictionary keys
         addresses = list(data.keys())
         logger.info(f"Loaded {len(addresses)} token addresses")
         return addresses

    async def get_token_symbols(self) -> Dict[str, str]:
        """Get the mapping of token addresses to symbols from the config file."""
        data = await self._load_json_safe(self.TOKEN_SYMBOLS, "token symbols")
        if not isinstance(data, dict):
            logger.error("Invalid format for token symbols: must be a dict")
            raise ValueError("Invalid format for token symbols: must be a dict")
        return data

    async def get_erc20_signatures(self) -> Dict[str, str]:
        """Load ERC20 function signatures from JSON."""
        data = await self._load_json_safe(self.ERC20_SIGNATURES, "ERC20 function signatures")
        if not isinstance(data, dict):
            logger.error("Invalid format for ERC20 signatures: must be a dict")
            raise ValueError("Invalid format for ERC20 signatures: must be a dict")
        return data

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """
        Safe configuration value access with default.
        """
        try:
            return getattr(self, key, default)
        except AttributeError:
            logger.warning(f"Configuration key '{key}' not found, using default: {default}")
            return default

    def get_all_config_values(self) -> Dict[str, Any]:
         """Returns all configuration values as a dictionary."""
         return vars(self)

    async def load_abi_from_path(self, path: Path) -> List[Dict[str, Any]]:
        """Load and return ABI content from a file path."""
        try:
            async with aiofiles.open(path, 'r') as f:
                content = await f.read()
                abi = json.loads(content)
                if not isinstance(abi, list):
                    raise ValueError(f"Invalid ABI format in {path}: not a list")
                return abi
        except Exception as e:
            logger.error(f"Failed to load ABI from {path}: {e}")
            raise

    async def load(self) -> None:
        """Load and validate all configuration data."""
        try:
            # Create required directories if they don't exist
            self._create_required_directories()

            # Load and validate critical ABIs (Now just loads paths)
            await self._load_critical_abis()

            # Validate API keys (Enhanced API key validation)
            await self._validate_api_keys()

            # Validate addresses
            self._validate_addresses()

            logger.info("Configuration loaded successfully")

        except Exception as e:
            logger.error(f"Configuration load failed: {str(e)}")
            raise

    def _create_required_directories(self) -> None:
        """Create necessary directories if they don't exist."""
        required_dirs = [
            self.LINEAR_REGRESSION_PATH,
            self.BASE_PATH / "abi",
            self.BASE_PATH / "utils"
        ]

        for directory in required_dirs:
            os.makedirs(directory, exist_ok=True)
            logger.debug(f"Ensured directory exists: {directory}")

    async def _load_critical_abis(self) -> None:
        """Load and validate critical ABIs (Now just loads paths)."""
        try:
            self.AAVE_FLASHLOAN_ABI_PATH = self._resolve_path("AAVE_FLASHLOAN_ABI") # Now storing path
            self.AAVE_POOL_ABI_PATH = self._resolve_path("AAVE_POOL_ABI") # Now storing path
        except Exception as e:
            raise RuntimeError(f"Failed to load critical ABIs: {e}")


    async def _validate_api_keys(self) -> None:
        """
        Validate required API keys are set and functional.
        Includes detailed logging for debugging and improved error handling.
        """
        required_keys = []

        # Etherscan and Infura API keys
        required_keys.append(('ETHERSCAN_API_KEY', "https://api.etherscan.io/api?module=stats&action=ethprice&apikey={key}"))  # Example Etherscan test URL
        required_keys.append(('INFURA_API_KEY', "https://mainnet.infura.io/v3/{key}"))  # Example Infura test URL

        # Validate all free-tier API keys
        if self.COINGECKO_API_KEY_TYPE == "free":
            for idx, api_key in enumerate(self.COINGECKO_FREE_API_KEYS):
                test_url = f"https://api.coingecko.com/api/v3/ping?x_cg_demo_api_key={api_key}"
                required_keys.append((f"COINGECKO_FREE_API_KEY_{idx}", test_url))

        # Validate paid API key if configured
        if self.COINGECKO_API_KEY_TYPE == "paid":
            test_url = f"https://pro-api.coingecko.com/api/v3/ping?x_cg_pro_api_key={key}"
            required_keys.append(('COINGECKO_PAID_API_KEY', test_url))

        # Other API keys
        required_keys.append(('COINMARKETCAP_API_KEY', "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest?CMC_PRO_API_KEY={key}&limit=1"))  # Example CMC test URL
        required_keys.append(('CRYPTOCOMPARE_API_KEY', "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD&api_key={key}"))  # Example CryptoCompare test URL

        # Validate each API key
        missing_keys = []
        invalid_keys = []  # To track invalid keys
        async with aiohttp.ClientSession() as session:  # Create a session for all requests
            for key_name, test_url_template in required_keys:
                api_key = getattr(self, key_name, None)
                if not api_key:
                    logger.warning(f"API key {key_name} is missing.")
                    missing_keys.append(key_name)
                    continue  # Skip to next key if missing

                test_url = test_url_template.format(key=api_key)
                logger.debug(f"Testing API key {key_name} with URL: {test_url}")
                try:
                    if key_name == "INFURA_API_KEY":
                        # Execute POST request for Infura API key validation
                        async with session.post(test_url, json={
                            "jsonrpc": "2.0",
                            "method": "eth_blockNumber",
                            "params": [],
                            "id": 1
                        }, timeout=10) as response:
                            if response.status == 200:  # Basic check for 200 OK status
                                logger.debug(f"API key {key_name} validated successfully.")
                            elif response.status == 401 or response.status == 403 or response.status == 404:
                                logger.error(f"API key {key_name} is invalid or unauthorized (Status: {response.status}).")
                                invalid_keys.append(f"{key_name} (Status: {response.status})")
                            else:
                                logger.error(f"API key {key_name} failed validation (Status: {response.status}).")
                                invalid_keys.append(f"{key_name} (Status: {response.status})")                        
                    else:
                        # Execute GET request for other API keys
                        async with session.get(test_url, timeout=10) as response:  # Timeout for test requests
                            if response.status == 200:  # Basic check for 200 OK status
                                logger.debug(f"API key {key_name} validated successfully.")
                            elif response.status == 401 or response.status == 403 or response.status == 404:
                                logger.error(f"API key {key_name} is invalid or unauthorized (Status: {response.status}).")
                                invalid_keys.append(f"{key_name} (Status: {response.status})")
                            else:
                                logger.error(f"API key {key_name} failed validation (Status: {response.status}).")
                                invalid_keys.append(f"{key_name} (Status: {response.status})")    

                except asyncio.TimeoutError:
                    logger.error(f"API key {key_name} validation timed out.")
                    invalid_keys.append(f"{key_name} (Error: Timeout)")
                except Exception as e:   # Catch any request exceptions
                    logger.error(f"API key {key_name} validation request failed: {e}") # Warning for request failures
                    invalid_keys.append(f"{key_name} (Error: {e})")

        # Log summary of missing and invalid keys
        if missing_keys:
            logger.warning(f"Missing API keys: {', '.join(missing_keys)}")
        if invalid_keys:
            logger.error(f"Invalid API keys: {', '.join(invalid_keys)}")  # Log invalid keys as errors
            raise ValueError(f"Invalid API keys detected: {', '.join(invalid_keys)}")  # Raise exception for invalid keys

    def _validate_addresses(self) -> None:
        """Validate all Ethereum addresses in configuration."""
        address_fields = [
            'WALLET_ADDRESS',
            'UNISWAP_ADDRESS',
            'SUSHISWAP_ADDRESS',
            'AAVE_POOL_ADDRESS',
            'AAVE_FLASHLOAN_ADDRESS'
        ]

        for field in address_fields:
            value = getattr(self, field, None)
            if value:
                try:
                    setattr(self, field,
                           self._validate_ethereum_address(value, field))
                except ValueError as e:
                    logger.error(f"Invalid address for {field}: {e}")
                    raise