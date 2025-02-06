#========================================================================================================================
# File: main_core.py
#========================================================================================================================
import asyncio
import time
import tracemalloc
import async_timeout

from typing import Any, Dict, List, Optional, Tuple, Union
from web3 import AsyncWeb3
from web3.eth import AsyncEth
from web3.middleware import ExtraDataToPOAMiddleware
from web3 import AsyncIPCProvider, AsyncHTTPProvider, WebSocketProvider
from eth_account import Account

from abi_registry import ABI_Registry
from api_config import API_Config
from configuration import Configuration
from market_monitor import Market_Monitor
from mempool_monitor import Mempool_Monitor
from nonce_core import Nonce_Core
from safety_net import Safety_Net
from strategy_net import Strategy_Net
from transaction_core import Transaction_Core
from colorama import init, Fore, Style
import logging as logger


logger = logger.getLogger(__name__)

# Constants

MIN_ETH_BALANCE = 0.01  # minimum ETH balance threshold
PROVIDER_TIMEOUT = 10  # seconds
WEB3_MAX_RETRIES = 3
WEB3_RETRY_DELAY = 2

# Custom types
ComponentType = Union[
    API_Config,
    Nonce_Core,
    Safety_Net,
    Market_Monitor,
    Mempool_Monitor,
    Transaction_Core,
    Strategy_Net
]

class Main_Core:
    """
    Builds and manages the entire MEV bot, initializing all components,
    managing connections, and orchestrating the main execution loop.
    """

    def __init__(self, configuration: "Configuration") -> None:
        """
        Initialize the main application core.

        Args:
            configuration: Configuration object containing settings.
        """
        tracemalloc.start()
        self.memory_snapshot = tracemalloc.take_snapshot()
        self.configuration = configuration
        self.web3: Optional[AsyncWeb3] = None
        self.account: Optional[Account] = None
        self.running: bool = False
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self.components: Dict[str, Optional[ComponentType]] = {
            'api_config': None,
            'nonce_core': None,
            'safety_net': None,
            'market_monitor': None,
            'mempool_monitor': None,
            'transaction_core': None,
            'strategy_net': None,
        }
        self._component_health: Dict[str, bool] = {name: False for name in self.components}

        # Assign constants as instance attributes
        self.WEB3_MAX_RETRIES = WEB3_MAX_RETRIES
        self.WEB3_RETRY_DELAY = WEB3_RETRY_DELAY
        self.PROVIDER_TIMEOUT = PROVIDER_TIMEOUT

        logger.info("Initializing 0xBuilder...")

    async def _initialize_components(self) -> None:
        """Initialize all components in the correct dependency order."""
        try:
            # 1. First initialize configuration and load ABIs
            logger.debug("Loading Configuration...")
            await self._load_configuration()
            logger.info("Configuration initialized ✅")
            # Initialize ABI Registry and load ABIs
            logger.debug("Initializing ABI Registry...")
            abi_registry = ABI_Registry()
            await abi_registry.initialize(base_path=self.configuration.BASE_PATH)  # Pass BASE_PATH here
            logger.info("ABI Registry initialized ✅")
            # Load and validate ERC20 ABI
            erc20_abi = await self._load_abi(self.configuration.ERC20_ABI, abi_registry)
            if not erc20_abi:
                raise ValueError("Failed to load ERC20 ABI")

            logger.debug("Initializing Web3...")
            self.web3 = await self._initialize_web3()
            logger.info("Web3 initialized ✅")
            if not self.web3:
                raise RuntimeError("Failed to initialize Web3 connection")

            self.account = Account.from_key(self.configuration.WALLET_KEY)
            await self._check_account_balance()
            logger.info("Account {self.account.address} initialized ✅")
            # 2. Initialize API config with context manager
            logger.debug("Initializing API_Config...")
            async with API_Config(self.configuration) as api_config:
                self.components['api_config'] = api_config
                await self.components['api_config'].initialize()
            logger.info("API_Config initialized ✅")

            # 3. Initialize nonce core
            logger.debug("Initializing Nonce_Core...")
            self.components['nonce_core'] = Nonce_Core(
                self.web3,
                self.account.address,
                self.configuration
            )
            await self.components['nonce_core'].initialize()
            logger.info(f"Nonce_Core initialized with nonce {Nonce_Core.get_nonce}✅")

            # 4. Initialize safety net
            logger.debug("Initializing Safety_Net...")
            self.components['safety_net'] = Safety_Net(
                self.web3,
                self.configuration,
                self.account,
                self.components['api_config'],
                 market_monitor = self.components.get('market_monitor')
            )
            await self.components['safety_net'].initialize()
            logger.info("Safety_Net initialized ✅ ")

            # 5. Initialize transaction core
            logger.debug("Initializing Transaction_Core...")
            self.components['transaction_core'] = Transaction_Core(
                web3=self.web3,
                account=self.account,
                AAVE_FLASHLOAN_ADDRESS=self.configuration.AAVE_FLASHLOAN_ADDRESS,
                AAVE_FLASHLOAN_ABI=self.configuration.AAVE_FLASHLOAN_ABI,
                AAVE_POOL_ADDRESS=self.configuration.AAVE_POOL_ADDRESS,
                AAVE_POOL_ABI=self.configuration.AAVE_POOL_ABI,
                api_config=self.components['api_config'],
                nonce_core=self.components['nonce_core'],
                safety_net=self.components['safety_net'],
                configuration=self.configuration
            )
            await self.components['transaction_core'].initialize()
            logger.info("Transaction_Core initialized ✅ ")

             # 6. Initialize market monitor
            logger.debug("Initializing Market_Monitor...")
            self.components['market_monitor'] = Market_Monitor(
                web3=self.web3,
                configuration=self.configuration,
                api_config=self.components['api_config'],
                transaction_core=self.components['transaction_core']
            )
            await self.components['market_monitor'].initialize()
            logger.info("Market_Monitor initialized ✅ ")

            # 7. Initialize mempool monitor with validated ABI
            logger.debug("Initializing Mempool_Monitor...")
            self.components['mempool_monitor'] = Mempool_Monitor(
                web3=self.web3,
                safety_net=self.components['safety_net'],
                nonce_core=self.components['nonce_core'],
                api_config=self.components['api_config'],
                monitored_tokens=await self.configuration.get_token_addresses(),
                configuration=self.configuration,
                erc20_abi=erc20_abi,
                market_monitor=self.components['market_monitor']
            )
            await self.components['mempool_monitor'].initialize()
            logger.info("Mempool_Monitor initialized ✅ ")

            # 8. Finally initialize strategy net
            logger.debug("Initializing Strategy_Net...")
            self.components['strategy_net'] = Strategy_Net(
                self.components['transaction_core'],
                self.components['market_monitor'],
                self.components['safety_net'],
                 self.components['api_config']
            )
            await self.components['strategy_net'].initialize()
            logger.info("Strategy_Net initialized ✅")

            logger.info("All components initialized successfully ✅")

        except Exception as e:
            logger.critical(f"Component initialization failed: {e}")
            raise

    async def __aenter__(self) -> "API_Config":
        """Context manager entry point."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit point."""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.debug("APIConfig session closed.")

    async def initialize(self) -> None:
        """Initialize all components with proper error handling."""
        try:
            before_snapshot = tracemalloc.take_snapshot()
            await self._initialize_components()
            after_snapshot = tracemalloc.take_snapshot()

            # Log memory usage
            top_stats = after_snapshot.compare_to(before_snapshot, 'lineno')
            logger.debug("Memory allocation during initialization:")
            for stat in top_stats[:3]:
                logger.debug(str(stat))

            logger.info("Main Core initialization complete ✅")

        except Exception as e:
            logger.critical(f"Main Core initialization failed: {e}")
            raise

    async def _load_configuration(self) -> None:
        """Load all configuration elements in the correct order."""
        try:
            # First load the configuration itself
            await self.configuration.load()

        except Exception as e:
            logger.critical(f"Failed to load configuration: {e}")
            raise

    async def _initialize_web3(self) -> Optional[AsyncWeb3]:
        """Initialize Web3 connection with error handling and retries."""
        providers = await self._get_providers()
        if not providers:
            logger.error("No valid endpoints provided!")
            return None

        for provider_name, provider in providers:
             for attempt in range(self.WEB3_MAX_RETRIES):
                try:
                    logger.debug(f"Attempting connection with {provider_name} (attempt {attempt + 1})...")
                    web3 = AsyncWeb3(provider, modules={"eth": (AsyncEth,)})

                    # Test connection with timeout
                    try:
                         async with async_timeout.timeout(10):
                             if await web3.is_connected():
                                 chain_id = await web3.eth.chain_id
                                 logger.info(f"Connected to network via {provider_name} (Chain ID: {chain_id})")
                                 await self._add_middleware(web3)
                                 return web3
                    except asyncio.TimeoutError:
                         logger.warning(f"Connection timeout with {provider_name}")
                         continue

                except Exception as e:
                    logger.warning(f"{provider_name} connection attempt {attempt + 1} failed: {e}")
                    if attempt < self.WEB3_MAX_RETRIES - 1:
                        await asyncio.sleep(self.WEB3_RETRY_DELAY * (attempt + 1))
                    continue

             logger.error(f"All attempts failed for {provider_name}")

        logger.error("Failed to initialize Web3 with any provider")
        return None

    async def _get_providers(self) -> List[Tuple[str, Union[AsyncIPCProvider, AsyncHTTPProvider, WebSocketProvider]]]:
        """Get list of available providers with validation."""
        providers = []

        if self.configuration.HTTP_ENDPOINT:
             try:
                http_provider = AsyncHTTPProvider(self.configuration.HTTP_ENDPOINT)
                await http_provider.make_request('eth_blockNumber', [])
                providers.append(("HTTP Provider", http_provider))
                logger.info("Linked to Ethereum network via HTTP Provider. ✅")
                return providers
             except Exception as e:
                 logger.warning(f"HTTP Provider failed. {e} ❌ - Attempting WebSocket... ")

        if self.configuration.WEBSOCKET_ENDPOINT:
            try:
                ws_provider = WebSocketProvider(self.configuration.WEBSOCKET_ENDPOINT)
                await ws_provider.connect()
                providers.append(("WebSocket Provider", ws_provider))
                logger.info("Linked to Ethereum network via WebSocket Provider. ✅")
                return providers
            except Exception as e:
                logger.warning(f"WebSocket Provider failed. {e} ❌ - Attempting IPC... ")

        if self.configuration.IPC_ENDPOINT:
            try:
                ipc_provider = AsyncIPCProvider(self.configuration.IPC_ENDPOINT)
                await ipc_provider.make_request('eth_blockNumber', [])
                providers.append(("IPC Provider", ipc_provider))
                logger.info("Linked to Ethereum network via IPC Provider. ✅")
                return providers
            except Exception as e:
                 logger.warning(f"IPC Provider failed. {e} ❌ - All providers failed.")

        logger.critical("No more providers are available! ❌")
        return providers

    async def _add_middleware(self, web3: AsyncWeb3) -> None:
        """Middleware based on network."""
        try:
            chain_id = await web3.eth.chain_id
            if (chain_id in {99, 100, 77, 7766, 56}):  # POA networks
                web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                logger.info(f"Injected POA middleware.")
            elif chain_id in {1, 3, 4, 5, 42, 420}:  # ETH networks
                logger.info(f"ETH network.")
                pass
            else:
                logger.warning(f"Unknown network; no middleware injected.")
        except Exception as e:
            logger.error(f"Middleware configuration failed: {e}")
            raise

    async def _check_account_balance(self) -> None:
        """Check account balance with improved error handling."""
        if not self.account or not self.web3:
            raise ValueError("Account or Web3 not initialized")

        try:
            balance = await self.web3.eth.get_balance(self.account.address)
            balance_eth = self.web3.from_wei(balance, 'ether')

            logger.info(f"Account {self.account.address[:8]}...{self.account.address[-6:]}")
            logger.info(f"Balance: {balance_eth:.4f} ETH")

            if balance_eth < MIN_ETH_BALANCE:
                logger.warning(f"Critical: Low account balance ({balance_eth:.4f} ETH)")
                # Could add automatic safety measures here

        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            raise


    async def _load_abi(self, abi_path: str, abi_registry: "ABI_Registry") -> List[Dict[str, Any]]:
        """Load contract ABI from a file with better path handling."""
        try:
            abi = abi_registry.get_abi('erc20')
            if not abi:
                raise ValueError("Failed to load ERC20 ABI using ABI Registry")
            return abi
        except Exception as e:
            logger.error(f"Error loading ABI from {abi_path}: {e}")
            raise

    async def run(self) -> None:
        """Enhanced main execution loop with component health monitoring."""
        logger.info("Starting 0xBuilder...")
        self.running = True
        initial_snapshot = tracemalloc.take_snapshot()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.components['mempool_monitor'].start_monitoring())
                tg.create_task(self._process_profitable_transactions())
                tg.create_task(self._monitor_memory(initial_snapshot))
                tg.create_task(self._check_component_health())

        except* asyncio.CancelledError:
            logger.info("Graceful shutdown initiated")
        except* Exception as e:
            logger.error(f"Fatal error in run loop: {e}")
        finally:
            await self.stop()

    async def _monitor_memory(self, initial_snapshot: tracemalloc.Snapshot) -> None:
        """Enhanced memory monitoring with leak detection."""
        last_snapshot = initial_snapshot
        while self.running:
            try:
                current_snapshot = tracemalloc.take_snapshot()
                diff_stats = current_snapshot.compare_to(last_snapshot, 'lineno')

                # Log significant memory changes
                significant_changes = [stat for stat in diff_stats if abs(stat.size_diff) > 1024 * 1024]  # > 1MB
                if significant_changes:
                    logger.warning("Significant memory changes detected:")
                    for stat in significant_changes[:3]:
                        logger.warning(str(stat))

                last_snapshot = current_snapshot
                await asyncio.sleep(self.configuration.MEMORY_CHECK_INTERVAL) # Use configurable interval
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Memory monitoring error: {e}")

    async def _check_component_health(self) -> None:
        """Periodic health check of all components."""
        while self.running:
            try:
                for name, component in self.components.items():
                    if component and hasattr(component, 'is_healthy'):
                        self._component_health[name] = await component.is_healthy()
                    else:
                        self._component_health[name] = component is not None # Assume healthy if no is_healthy()

                if not all(self._component_health.values()):
                    unhealthy = [name for name, healthy in self._component_health.items() if not healthy]
                    logger.warning(f"Unhealthy components detected: {unhealthy}")

                await asyncio.sleep(self.configuration.COMPONENT_HEALTH_CHECK_INTERVAL) # Use configurable interval
            except Exception as e:
                logger.error(f"Health check error: {e}")
                await asyncio.sleep(5)  # Back off on error

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.debug("APIConfig session manually closed.")

    async def stop(self) -> None:
        """Enhanced graceful shutdown."""
        if not self.running:
            return

        logger.warning("Initiating graceful shutdown...")
        self.running = False
        self._shutdown_event.set()

        try:
            # Stop components in parallel with timeout
            stop_tasks = []
            for name, component in self.components.items():
                if component and hasattr(component, 'stop'):
                    task = asyncio.create_task(self._stop_component(name, component))
                    stop_tasks.append(task)

            if stop_tasks:
                done, pending = await asyncio.wait(stop_tasks, timeout=10)
                if pending:
                    logger.warning(f"Forcing {len(pending)} component(s) to stop")
                    for task in pending:
                        task.cancel()

            # Cleanup web3
            if self.web3 and hasattr(self.web3.provider, 'disconnect'):
                await self.web3.provider.disconnect()

            # Final memory report
            self._log_final_memory_stats()

        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        finally:
            tracemalloc.stop()
            logger.info("Shutdown complete")

    def _log_final_memory_stats(self) -> None:
        """Log final memory statistics."""
        try:
            final_snapshot = tracemalloc.take_snapshot()
            top_stats = final_snapshot.compare_to(self.memory_snapshot, 'lineno')

            logger.debug("Final memory allocation changes:")
            for stat in top_stats[:5]:
                logger.debug(str(stat))
        except Exception as e:
            logger.error(f"Error logging final memory stats: {e}")

    async def _stop_component(self, name: str, component: Any) -> None:
        """Stop a single component with error handling."""
        try:
            await component.stop()
            logger.debug(f"Stopped {name}")
        except Exception as e:
            logger.error(f"Error stopping {name}: {e}")

    async def _process_profitable_transactions(self) -> None:
        """Process profitable transactions from the queue."""
        strategy = self.components['strategy_net']
        monitor = self.components['mempool_monitor']

        while self.running:
            try:
                try:
                    tx = await asyncio.wait_for(monitor.profitable_transactions.get(), timeout=self.configuration.PROFITABLE_TX_PROCESS_TIMEOUT) # Use configurable timeout
                    tx_hash = tx.get('tx_hash', 'Unknown')
                    strategy_type = tx.get('strategy_type', 'Unknown')
                except asyncio.TimeoutError:
                    continue

                logger.debug(f"Processing transaction {tx_hash} with strategy type {strategy_type}")
                success = await strategy.execute_best_strategy(tx, strategy_type)

                if success:
                    logger.debug(f"Strategy execution successful for tx: {tx_hash}")
                else:
                    logger.warning(f"Strategy execution failed for tx: {tx_hash}")

                # Mark task as done
                monitor.profitable_transactions.task_done()

            except Exception as e:
                logger.error(f"Error processing transaction: {e}", exc_info=True) # error logging with traceback