#========================================================================================================================
# File: mempool_monitor.py
#========================================================================================================================
import asyncio
import time
import async_timeout
import hexbytes as hexbytes

from decimal import Decimal
from typing import Any, Dict, List, Optional
from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound
from web3.exceptions import Web3ValueError

from abi_registry import ABI_Registry
from api_config import API_Config
from configuration import Configuration
from market_monitor import Market_Monitor
from nonce_core import Nonce_Core
from safety_net import Safety_Net
import logging as logger

logger = logger.getLogger(__name__)

class Mempool_Monitor:
    """
    Advanced mempool monitoring system that identifies and analyzes profitable transactions.
    Includes sophisticated profit estimation, caching, and parallel processing capabilities.
    """

    def __init__(
        self,
        web3: AsyncWeb3,
        safety_net: "Safety_Net",
        nonce_core: "Nonce_Core",
        api_config: "API_Config",
        monitored_tokens: Optional[List[str]] = None,
        configuration: Optional["Configuration"] = None,
        erc20_abi: Optional[List[Dict[str, Any]]] = None,
        market_monitor: Optional["Market_Monitor"] = None
    ):
        """
        Initialize the Mempool Monitor.

        Args:
            web3: An AsyncWeb3 instance.
            safety_net: Safety Net instance.
            nonce_core: Nonce Core instance.
            api_config: API Config instance.
            monitored_tokens: A list of token addresses to monitor.
            configuration: Configuration object containing settings.
            erc20_abi: ERC20 ABI object
            market_monitor: Market Monitor instance.
        """
        # Core components
        self.web3 = web3
        self.configuration = configuration
        self.safety_net = safety_net
        self.nonce_core = nonce_core
        self.api_config = api_config
        self.market_monitor = market_monitor

        # Monitoring state
        self.running = False
        self.pending_transactions = asyncio.Queue()
        self.monitored_tokens = set(monitored_tokens or [])
        self.processed_transactions_lock = asyncio.Lock()
        self.profitable_transactions = asyncio.Queue()
        self.processed_transactions = set()

        # Configuration
        # Validate ERC20 ABI
        if not erc20_abi or not isinstance(erc20_abi, list):
            logger.error("Invalid or missing ERC20 ABI")
            self.erc20_abi = []
        else:
            self.erc20_abi = erc20_abi
            logger.debug(f"Loaded ERC20 ABI with {len(self.erc20_abi)} entries")
        self.minimum_profit_threshold = Decimal("0.001")
        self.backoff_factor = 1.5

        # Concurrency control
        self.semaphore = asyncio.Semaphore(configuration.MEMPOOL_MAX_PARALLEL_TASKS) 
        self.task_queue = asyncio.Queue()

        # Function signature mappings
        self.function_signatures = {
            '0xa9059cbb': 'transfer',
            '0x095ea7b3': 'approve',
            '0x23b872dd': 'transferFrom',
        }

        logger.info("Go for main engine start! ✅...")
        asyncio.sleep(1) 

        self.abi_registry = ABI_Registry()

    async def __aenter__(self) -> "Mempool_Monitor":
        """Context manager entry point."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit point."""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.debug("MempoolMonitor session closed.")

    async def initialize(self) -> None:
        """
        Initialize the Mempool Monitor.

        This method performs the following initializations:
        - Updates ERC20 function signatures if available in the configuration.
        - Loads the ERC20 ABI through the ABI manager.
        - Validates the required ERC20 methods.
        - Initializes monitoring state attributes such as running status,
          pending transactions queue, profitable transactions queue,
          processed transactions set, and task queue.
        - Logs the initialization status.
        """
        try:
            logger.debug("Initializing MempoolMonitor...")
        
            # Debugging: Log the state of the Configuration object
            if self.configuration:
                logger.debug(f"Configuration data: {self.configuration.config_data}")
                logger.debug(f"MEMPOOL_MAX_RETRIES: {self.configuration.MEMPOOL_MAX_RETRIES}")
                        
            # Update ERC20 signatures if available
            if self.configuration and hasattr(self.configuration, 'get_erc20_signatures'):
                self.function_signatures.update(await self.configuration.get_erc20_signatures())
            # Load ERC20 ABI through ABI manager
            if not self.erc20_abi:
                raise ValueError("Failed to load ERC20 ABI")

            # Validate required methods
            required_methods = ['transfer', 'approve', 'transferFrom', 'balanceOf']
            if not self.abi_registry._validate_abi(self.erc20_abi, 'erc20'):
                 raise ValueError("Invalid ERC20 ABI")

            # Initialize other attributes
            self.running = False
            self.pending_transactions = asyncio.Queue()
            self.profitable_transactions = asyncio.Queue()
            self.processed_transactions = set()
            self.task_queue = asyncio.Queue()

            logger.info("MempoolMonitor initialized ✅")

        except Exception as e:
            logger.critical(f"Mempool Monitor initialization failed: {e}")
            raise

    async def start_monitoring(self) -> None:
         """Start monitoring the mempool with  error handling."""
         if self.running:
            logger.debug("Monitoring is already active.")
            return
         try:
            self.running = True
            monitoring_task = asyncio.create_task(self._run_monitoring())
            processor_task = asyncio.create_task(self._process_task_queue())

            logger.info("Lift-off 🚀🚀🚀")
            logger.info("Monitoring mempool activities... 📡")
            await asyncio.gather(monitoring_task, processor_task)

         except Exception as e:
            self.running = False
            logger.error(f"Error during monitoring start: {e}")

    async def _run_monitoring(self) -> None:
        """ mempool monitoring with automatic recovery and fallback."""
        retry_count = 0

        while self.running:
            try:
                # Try setting up filter first
                try:
                    pending_filter = await self._setup_pending_filter()
                    if pending_filter:
                        while self.running:
                            tx_hashes = await pending_filter.get_new_entries()
                            await self._handle_new_transactions(tx_hashes)
                            await asyncio.sleep(1)  # Prevent tight loop
                    else:
                        # Fallback to polling if filter not available
                        await self._poll_pending_transactions()
                except Exception as filter_error:
                    logger.warning(f"Filter-based monitoring failed: {filter_error}")
                    logger.info("Switching to polling-based monitoring...")
                    await self._poll_pending_transactions()

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                retry_count += 1
                await asyncio.sleep(self.configuration.MEMPOOL_RETRY_DELAY * retry_count) # Use configurable RETRY_DELAY

    async def _poll_pending_transactions(self) -> None:
        """ polling method with better error handling."""
        last_block = await self.web3.eth.block_number

        while self.running:
            try:
                current_block = await self.web3.eth.block_number
                if current_block <= last_block:
                    await asyncio.sleep(1)
                    continue

                # Process new blocks
                for block_num in range(last_block + 1, current_block + 1):
                    try:
                        block = await self.web3.eth.get_block(block_num, full_transactions=True)
                        if block and block.transactions:
                            tx_hashes = [tx.hash.hex() if hasattr(tx, 'hash') else tx['hash'].hex()
                                       for tx in block.transactions]
                            await self._handle_new_transactions(tx_hashes)
                    except Exception as e:
                        logger.error(f"Error processing block {block_num}: {e}")
                        continue

                last_block = current_block
                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
                await asyncio.sleep(2)

    async def _setup_pending_filter(self) -> Optional[Any]:
        """Set up pending transaction filter with validation and fallback."""
        try:
            # Try to create a filter
            pending_filter = await self.web3.eth.filter("pending")

            # Test filter with timeout
            try:
                async with async_timeout.timeout(5):
                    await pending_filter.get_new_entries()
                    logger.debug("Successfully set up pending transaction filter")
                    return pending_filter
            except asyncio.TimeoutError:
                logger.warning("Filter setup timed out, falling back to polling")
                return None
            except Exception as e:
                logger.warning(f"Filter validation failed: {e}, retrying after delay")
                await asyncio.sleep(self.configuration.MEMPOOL_RETRY_DELAY) # Use configurable RETRY_DELAY
                return None

        except Exception as e:
            logger.warning(f"Failed to setup pending filter: {e}, falling back to polling")
            return None

    async def _handle_new_transactions(self, tx_hashes: List[str]) -> None:
        """Process new transactions in parallel with rate limiting."""
        async def process_batch(batch):
             await asyncio.gather(
                *(self._queue_transaction(tx_hash) for tx_hash in batch)
             )
        try:
            # Process transactions in batches
            for i in range(0, len(tx_hashes), self.configuration.MEMPOOL_BATCH_SIZE): # Use configurable BATCH_SIZE
                batch = tx_hashes[i: i + self.configuration.MEMPOOL_BATCH_SIZE]
                await process_batch(batch)

        except Exception as e:
            logger.error(f"Error handling new transactions: {e}")

    async def _queue_transaction(self, tx_hash: str) -> None:
        """Queue transaction for processing with deduplication."""
        tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        async with self.processed_transactions_lock:
            if tx_hash_hex not in self.processed_transactions:
                self.processed_transactions.add(tx_hash_hex)
                await self.task_queue.put(tx_hash_hex)

    async def _process_task_queue(self) -> None:
        """Process queued transactions with concurrency control."""
        while self.running:
            try:
                tx_hash = await self.task_queue.get()
                async with self.semaphore: 
                    await self.process_transaction(tx_hash)
                self.task_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing task queue: {e}")

    async def process_transaction(self, tx_hash: str) -> None:
        """Process individual transactions with  error handling."""
        try:
            tx = await self._get_transaction_with_retry(tx_hash)
            if not tx:
                return

            analysis = await self.analyze_transaction(tx)
            if analysis.get("is_profitable"):
                await self._handle_profitable_transaction(analysis)

        except Exception as e:
             logger.debug(f"Error processing transaction {tx_hash}: {e}")

    async def _get_transaction_with_retry(self, tx_hash: str) -> Optional[Any]:
        """Fetch transaction details with exponential backoff."""
        for attempt in range(self.configuration.MEMPOOL_MAX_RETRIES): # Use configurable MAX_RETRIES
            try:
                return await self.web3.eth.get_transaction(tx_hash)
            except TransactionNotFound:
                 if attempt == self.configuration.MEMPOOL_MAX_RETRIES - 1:
                    return None
                 await asyncio.sleep(self.configuration.MEMPOOL_RETRY_DELAY * (attempt + 1)) # Use configurable RETRY_DELAY
            except Exception as e:
                logger.error(f"Error fetching transaction {tx_hash}: {e}")
                return None

    async def _handle_profitable_transaction(self, analysis: Dict[str, Any]) -> None:
        """
        Handle profitable transactions with  validation and logging.

        This method performs the following steps:
        - Validates the profit value from the analysis dictionary.
        - Formats the profit value for logging purposes.
        - Adds additional analysis data such as timestamp, gas price, and strategy type.
        - Puts the analysis dictionary into the profitable transactions queue.
        - Logs the details of the profitable transaction.

        Args:
            analysis: A dictionary containing the analysis of the transaction, including
                      the estimated profit, transaction hash, gas price, and other relevant details.

        Raises:
            Exception: If an error occurs during the handling process, it is logged.
        """
        try:
            # Validate profit value
            profit = analysis.get('profit', Decimal(0))
            if isinstance(profit, (int, float)):
                profit = Decimal(str(profit))
            elif not isinstance(profit, Decimal):
                logger.warning(f"Invalid profit type: {type(profit)}")
                profit = Decimal(0)

            # Format profit for logging
            profit_str = f"{float(profit):.6f}" if profit > 0 else 'Unknown'

            # Additional analysis data
            analysis['profit'] = profit
            analysis['timestamp'] = time.time()
            analysis['gas_price'] = self.web3.from_wei(
                analysis.get('gasPrice', 0),
                'gwei'
            )

            # Strategy type
            analysis['strategy_type'] = self._determine_strategy_type(analysis)

            await self.profitable_transactions.put(analysis)

            logger.info(
                f"Profitable transaction identified: {analysis['tx_hash']} "
                f"(Estimated profit: {profit_str} ETH, Strategy Type: {analysis['strategy_type']})"
            )

        except Exception as e:
            logger.error(f"Error handling profitable transaction: {e}")

    def _determine_strategy_type(self, analysis: Dict[str, Any]) -> str:
        """Determine the correct strategy type based on the transaction details."""

        if analysis.get('value', 0) > 0 and 'input' not in analysis:
              return "eth_transaction"
        if  analysis.get('function_name') in ("swap", "swapExactTokensForTokens", "swapTokensForExactTokens"):
              # Simple heuristic to identify different types of swaps
                if "amountOutMin" in analysis.get('params', {}):
                  if 'path' in analysis.get('params', {}):
                       return "sandwich_attack"
                  return "back_run"
                return "front_run"
        return "unknown"


    async def analyze_transaction(self, tx) -> Dict[str, Any]:
        """
        Analyze a transaction to determine if it is profitable.

        This method performs the following steps:
        - Checks if the transaction has a hash and input data.
        - Determines if the transaction involves ETH or tokens.
        - Analyzes the transaction based on its type (ETH or token).
        - Returns a dictionary indicating whether the transaction is profitable.

        Args:
            tx: The transaction object to be analyzed.

        Returns:
            A dictionary containing the analysis results, including:
            - "is_profitable": A boolean indicating if the transaction is profitable.
            - Additional details such as transaction hash, value, gas price, etc.
        """
        if not tx.hash or not tx.input:
            logger.debug(
                f"Transaction {tx.hash.hex()} is missing essential fields. Skipping."
            )
            return {"is_profitable": False}
        try:
            if tx.value > 0:
                return await self._analyze_eth_transaction(tx)
            return await self._analyze_token_transaction(tx)
        except Exception as e:
            logger.error(
                f"Error analyzing transaction {tx.hash.hex()}: {e}"
            )
            return {"is_profitable": False}

    async def _analyze_eth_transaction(self, tx) -> Dict[str, Any]:
        try:
            if await self._is_profitable_eth_transaction(tx):
                await self._log_transaction_details(tx, is_eth=True)
                return {
                    "is_profitable": True,
                    "tx_hash": tx.hash.hex(),
                    "value": tx.value,
                    "to": tx.to,
                    "from": tx["from"],
                    "input": tx.input,
                    "gasPrice": tx.gasPrice,
                }
            return {"is_profitable": False}
        except Exception as e:
            logger.error(
                f"Error analyzing ETH transaction {tx.hash.hex()}: {e}"
            )
            return {"is_profitable": False}

    async def _analyze_token_transaction(self, tx) -> Dict[str, Any]:
        """
         token transaction analysis with better validation.

        This method performs the following steps:
        - Validates the presence of the ERC20 ABI and transaction input data.
        - Extracts the function selector from the transaction input.
        - Attempts to decode the transaction using local signature lookup and contract ABI.
        - Validates and extracts transaction parameters based on the function name.
        - Estimates the potential profit of the transaction.
        - Returns a dictionary indicating whether the transaction is profitable.

        Args:
            tx: The transaction object to be analyzed.

        Returns:
            A dictionary containing the analysis results, including:
            - "is_profitable": A boolean indicating if the transaction is profitable.
            - Additional details such as transaction hash, value, gas price, etc.
        """
        try:
            if not self.erc20_abi or not tx.input or len(tx.input) < 10:
                logger.debug("Missing ERC20 ABI or invalid transaction input")
                return {"is_profitable": False}

            # Extract function selector
            function_selector = tx.input[:10]
            selector_no_prefix = function_selector[2:]

            # Initialize variables
            function_name = None
            function_params = {}
            decoded = False

            # Method 1: Try local signature lookup first (fastest)
            if selector_no_prefix in self.function_signatures:
                try:
                    function_name = self.function_signatures[selector_no_prefix]
                    if len(tx.input) >= 138:  # Standard ERC20 call length
                        params_data = tx.input[10:]
                        if function_name == 'transfer':
                            to_address = '0x' + params_data[:64][-40:]
                            amount = int(params_data[64:128], 16)
                            function_params = {'to': to_address, 'amount': amount}
                            decoded = True
                except Exception as e:
                    logger.debug(f"Error in direct signature lookup: {e}")

            # Method 2: Try contract decode only if direct lookup failed
            if not decoded:
                try:
                    # Ensure we have a valid contract address and ABI
                    if not tx.to or not self.erc20_abi:
                        logger.debug("Missing contract address or ABI")
                        return {"is_profitable": False}

                    contract = self.web3.eth.contract(
                        address=self.web3.to_checksum_address(tx.to),
                        abi=self.erc20_abi
                    )

                    try:
                        func_obj, decoded_params = contract.decode_function_input(tx.input)
                        function_name = (
                            getattr(func_obj, 'fn_name', None) or
                            getattr(func_obj, 'function_identifier', None)
                        )
                        if function_name:
                            function_params = decoded_params
                            decoded = True
                    except Web3ValueError as e:
                        logger.debug(f"Could not decode function input: {e}")
                except Exception as e:
                    logger.debug(f"Contract decode error: {e}")

            # Process decoded transaction if successful
            if decoded and function_name in ('transfer', 'transferFrom', 'swap', 'swapExactTokensForTokens', 'swapTokensForExactTokens'):
                #  parameter validation
                params = await self._extract_transaction_params(tx, function_name, function_params)
                if not params:
                    logger.debug(f"Could not extract valid parameters for {function_name}")
                    return {"is_profitable": False}

                amounts = await self._validate_token_amounts(params)
                if not amounts['valid']:
                    logger.debug(f"Invalid token amounts: {amounts['reason']}")
                    if 'details' in amounts:
                        logger.debug(f"Validation details: {amounts['details']}")
                    return {"is_profitable": False}

                estimated_profit = await self._estimate_profit(tx, function_params)
                if estimated_profit > self.minimum_profit_threshold:
                    return {
                        "is_profitable": True,
                        "profit": estimated_profit,
                        "function_name": function_name,
                        "params": function_params,
                        "tx_hash": tx.hash.hex(),
                        "to": tx.to,
                        "input": tx.input,
                        "value": tx.value,
                        "gasPrice": tx.gasPrice,
                    }

            if not decoded:
                logger.debug(f"Could not decode transaction with selector: {function_selector}")

            return {"is_profitable": False}

        except Exception as e:
            logger.error(f"Error analyzing token transaction {tx.hash.hex()}: {e}")
            return {"is_profitable": False}

    async def _is_profitable_eth_transaction(self, tx) -> bool:
        try:
            potential_profit = await self._estimate_eth_transaction_profit(tx)
            return potential_profit > self.minimum_profit_threshold
        except Exception as e:
            logger.debug(
                f"Error estimating ETH transaction profit for transaction {tx.hash.hex()}: {e}"
            )
            return False

    async def _estimate_eth_transaction_profit(self, tx: Any) -> Decimal:
        """
        Estimate the potential profit of an ETH transaction.

        This method performs the following steps:
        - Retrieves the dynamic gas price from the safety net.
        - Estimates the gas used by the transaction if not already provided.
        - Calculates the total gas cost in ETH.
        - Converts the transaction value from Wei to ETH.
        - Computes the potential profit by subtracting the gas cost from the transaction value.
        - Ensures the profit is non-negative.

        Args:
            tx: The transaction object to be analyzed.

        Returns:
            A Decimal representing the estimated profit in ETH.
        """
        try:
            gas_price_gwei = await self.safety_net.get_dynamic_gas_price()
            gas_used = tx.gas if tx.gas else await self.web3.eth.estimate_gas(tx)
            gas_cost_eth = Decimal(gas_price_gwei) * Decimal(gas_used) * Decimal("1e-9")
            eth_value = Decimal(self.web3.from_wei(tx.value, "ether"))
            potential_profit = eth_value - gas_cost_eth
            return potential_profit if potential_profit > 0 else Decimal(0)
        except Exception as e:
            logger.error(f"Error estimating ETH transaction profit: {e}")
            return Decimal(0)

    async def _estimate_profit(self, tx, function_params: Dict[str, Any]) -> Decimal:
        """ profit estimation with  precision and market analysis."""
        try:
            # Validate and get gas costs with increased precision
            gas_data = await self._calculate_gas_costs(tx)
            if not gas_data['valid']:
                logger.debug(f"Invalid gas data: {gas_data['reason']}")
                return Decimal(0)

            # Get token amounts with validation
            amounts = await self._validate_token_amounts(function_params)
            if not amounts['valid']:
                logger.debug(f"Invalid token amounts: {amounts['reason']}")
                return Decimal(0)

            # Get market data with validation
            market_data = await self._get_market_data(function_params['path'][-1])
            if not market_data['valid']:
                logger.debug(f"Invalid market data: {market_data['reason']}")
                return Decimal(0)

            # Calculate profit with all factors
            profit = await self._calculate_final_profit(
                amounts=amounts['data'],
                gas_costs=gas_data['data'],
                market_data=market_data['data']
            )

            # Log comprehensive calculation details
            self._log_profit_calculation(profit, amounts['data'], gas_data['data'], market_data['data'])

            return Decimal(max(0, profit))

        except Exception as e:
            logger.error(f"Error in profit estimation: {e}")
            return Decimal(0)

    async def _calculate_gas_costs(self, tx: Any) -> Dict[str, Any]:
        """
        Calculate gas costs with  precision.

        This method performs the following steps:
        - Retrieves the gas price from the transaction.
        - Converts the gas price from Wei to Gwei.
        - Estimates the gas used by the transaction if not already provided.
        - Adds a safety margin to the gas estimate (10%).
        - Calculates the total gas cost in ETH with high precision.
        - Returns a dictionary containing the gas cost details:
        - 'valid': A boolean indicating if the gas cost calculation was successful.
        - 'data': A dictionary with the following keys if 'valid' is True:
            - 'gas_price_gwei': The gas price in Gwei.
            - 'gas_used': The estimated gas used by the transaction.
            - 'gas_with_margin': The gas used with a safety margin.
            - 'gas_cost_eth': The total gas cost in ETH.
        - 'reason': A string indicating the reason for failure if 'valid' is False.
        """
        try:
            gas_price_wei = Decimal(tx.gasPrice)
            gas_price_gwei = Decimal(self.web3.from_wei(gas_price_wei, "gwei"))

            # Get dynamic gas estimate
            gas_used = tx.gas if tx.gas else await self.web3.eth.estimate_gas(tx)
            gas_used = Decimal(gas_used)

            # Safety margin for gas estimation (10%)
            gas_with_margin = gas_used * Decimal("1.1")

            # Calculate total gas cost in ETH
            gas_cost_eth = (gas_price_gwei * gas_with_margin * Decimal("1e-9")).quantize(Decimal("0.000000001"))

            return {
                'valid': True,
                'data': {
                    'gas_price_gwei': gas_price_gwei,
                    'gas_used': gas_used,
                    'gas_with_margin': gas_with_margin,
                    'gas_cost_eth': gas_cost_eth
                }
            }
        except Exception as e:
            return {'valid': False, 'reason': str(e)}

    async def _validate_token_amounts(self, function_params: Dict[str, Any]) -> Dict[str, Any]:
        """ token amount validation with better hex value handling."""
        try:
            # Extract amounts with more comprehensive fallbacks
            input_amount = function_params.get("amountIn",
                function_params.get("value",
                function_params.get("amount",
                function_params.get("_value", 0)
            )))
            output_amount = function_params.get("amountOutMin",
                function_params.get("amountOut",
                function_params.get("amount",
                function_params.get("_amount", 0)
                )
                )
            )

            #  hex string handling
            def parse_amount(amount: Any) -> int:
                if isinstance(amount, str):
                    if amount.startswith("0x"):
                        return int(amount, 16)
                    if amount.isnumeric():
                        return int(amount)
                return int(amount) if amount else 0

            # Parse amounts
            try:
                input_amount_wei = Decimal(str(parse_amount(input_amount)))
                output_amount_wei = Decimal(str(parse_amount(output_amount)))
            except (ValueError, TypeError) as e:
                return {
                    'valid': False, 'reason': f'Amount parsing error: {str(e)}',
                    'details': {
                        'input_raw': input_amount,
                        'output_raw': output_amount
                    }
                }

            # Validate amounts
            if input_amount_wei <= 0 and output_amount_wei <= 0:
                return {
                    'valid': False,
                    'reason': 'Both input and output amounts are zero or negative',
                    'details': {
                        'input_wei': str(input_amount_wei),
                        'output_wei': str(output_amount_wei)
                    }
                }

            # Convert to ETH with higher precision
            input_amount_eth = Decimal(self.web3.from_wei(input_amount_wei, "ether")).quantize(Decimal("0.000000001"))
            output_amount_eth = Decimal(self.web3.from_wei(output_amount_wei, "ether")).quantize(Decimal("0.000000001"))

            return {
                'valid': True,
                'data': {
                    'input_eth': input_amount_eth,
                    'output_eth': output_amount_eth,
                    'input_wei': input_amount_wei,
                    'output_wei': output_amount_wei
                }
            }

        except Exception as e:
            logger.error(f"Unexpected error in token amount validation: {e}")
            return {
                'valid': False,
                'reason': 'Unknown validation error',
                'details': {
                    'error': str(e),
                    'params': str(function_params)
                }
            }

    async def _get_market_data(self, token_address: str) -> Dict[str, Any]:
        """
        Get comprehensive market data for profit calculation.

        This method performs the following steps:
        - Retrieves the token symbol using the token address.
        - Fetches the real-time price of the token from the API.
        - Calculates dynamic slippage based on the token's trading volume.
        - Returns a dictionary containing the market data, including price and slippage.

        Args:
            token_address: The address of the token for which market data is being retrieved.

        Returns:
            A dictionary containing the market data:
            - 'valid': A boolean indicating if the market data retrieval was successful.
            - 'data': A dictionary with the following keys if 'valid' is True:
                - 'price': The real-time price of the token.
                - 'slippage': The calculated slippage based on trading volume.
                - 'symbol': The symbol of the token.
            - 'reason': A string indicating the reason for failure if 'valid' is False.
        """
        try:
            token_symbol = await self.api_config.get_token_symbol(self.web3, token_address)
            if not token_symbol:
                return {'valid': False, 'reason': 'Token symbol not found'}

            # Get market price and liquidity data
            price = await self.api_config.get_real_time_price(token_symbol.lower())
            if not price or price <= 0:
                return {'valid': False, 'reason': 'Invalid market price'}

            # Calculate dynamic slippage based on liquidity
            slippage = await self._calculate_dynamic_slippage(token_symbol)

            return {
                'valid': True,
                'data': {
                    'price': Decimal(str(price)),
                    'slippage': slippage,
                    'symbol': token_symbol
                }
            }
        except Exception as e:
            return {'valid': False, 'reason': str(e)}

    async def _calculate_dynamic_slippage(self, token_symbol: str) -> Decimal:
        """Calculate dynamic slippage based on market conditions."""
        try:
            volume = await self.api_config.get_token_volume(token_symbol)
            # Adjust slippage based on volume (higher volume = lower slippage)
            if (volume > 1_000_000):  # High volume
                return Decimal("0.995")  # 0.5% slippage
            elif volume > 500_000:  # Medium volume
                return Decimal("0.99")   # 1% slippage
            else:  # Low volume
                return Decimal("0.98")   # 2% slippage
        except Exception:
            return Decimal("0.99")  # Default to 1% slippage

    async def _calculate_final_profit(
        self,
        amounts: Dict[str, Decimal],
        gas_costs: Dict[str, Decimal],
        market_data: Dict[str, Any]
    ) -> Decimal:
        """
        Calculate final profit with all factors considered.

        This method performs the following steps:
        - Calculates the expected output value by multiplying the output amount in ETH by the market price and applying slippage.
        - Computes the net profit by subtracting the input amount in ETH and the gas cost in ETH from the expected output value.
        - Ensures the profit is non-negative and returns the final profit value.

        Args:
            amounts: A dictionary containing the input and output amounts in ETH and Wei.
            gas_costs: A dictionary containing the gas price in Gwei, gas used, gas with margin, and gas cost in ETH.
            market_data: A dictionary containing the market price, slippage, and token symbol.

        Returns:
            A Decimal representing the final profit in ETH.
        """
        try:
            # Calculate expected output value with slippage
            expected_output_value = (
                amounts['output_eth'] *
                market_data['price'] *
                market_data['slippage']
            ).quantize(Decimal("0.000000001"))

            # Calculate net profit
            profit = (
                expected_output_value -
                amounts['input_eth'] -
                gas_costs['gas_cost_eth']
            ).quantize(Decimal("0.000000001"))

            return profit

        except Exception as e:
            logger.error(f"Error in final profit calculation: {e}")
            return Decimal(0)

    def _log_profit_calculation(
        self,
        profit: Decimal,
        amounts: Dict[str, Decimal],
        gas_costs: Dict[str, Decimal],
        market_data: Dict[str, Any]
    ) -> None:
        """Log detailed profit calculation metrics."""
        logger.debug(
            f"Profit Calculation Details:\n"
            f"Token: {market_data['symbol']}\n"
            f"Input Amount: {amounts['input_eth']:.9f} ETH\n"
            f"Expected Output: {amounts['output_eth']:.9f} tokens\n"
            f"Market Price: {market_data['price']:.9f}\n"
            f"Slippage: {(1 - float(market_data['slippage'])) * 100:.2f}%\n"
            f"Gas Cost: {gas_costs['gas_cost_eth']:.9f} ETH\n"
            f"Gas Price: {gas_costs['gas_price_gwei']:.2f} Gwei\n"
            f"Final Profit: {profit:.9f} ETH"
        )

    async def _log_transaction_details(self, tx, is_eth=False) -> None:
        try:
            transaction_info = {
                "transaction hash": tx.hash.hex(),
                "value": self.web3.from_wei(tx.value, "ether") if is_eth else tx.value,
                "from": tx["from"],
                "to": (tx.to[:10] + "..." + tx.to[-10:]) if tx.to else None,
                "input": tx.input,
                "gas price": self.web3.from_wei(tx.gasPrice, "gwei"),
            }
            if is_eth:
                logger.debug(f"Pending ETH Transaction Details: {transaction_info}")
            else:
                logger.debug(f"Pending Token Transaction Details: {transaction_info}")
        except Exception as e:
            logger.debug(
                f"Error logging transaction details for {tx.hash.hex()}: {e}"
            )

    async def _extract_transaction_params(
        self,
        tx: Any,
        function_name: str,
        decoded_params: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Extract and validate transaction parameters with  parsing.
        """
        try:
            params = {}

            #  transfer parameter handling
            if function_name in ['transfer', 'transferFrom']:
                # Handle different parameter naming conventions
                amount = decoded_params.get('amount',
                            decoded_params.get('_value',
                                decoded_params.get('value',
                                    decoded_params.get('wad', 0))))
                to_addr = decoded_params.get('to',
                            decoded_params.get('_to',
                                decoded_params.get('dst',
                                    decoded_params.get('recipient'))))

                if function_name == 'transferFrom':
                    from_addr = decoded_params.get('from',
                                decoded_params.get('_from',
                                    decoded_params.get('src',
                                        decoded_params.get('sender'))))
                    params['from'] = from_addr

                params.update({
                    'amount': amount,
                    'to': to_addr
                })

            #  swap parameter handling
            elif function_name in ['swap', 'swapExactTokensForTokens', 'swapTokensForExactTokens']:
                # Handle different swap parameter formats
                params = {
                    'amountIn': decoded_params.get('amountIn',
                                decoded_params.get('amount0',
                                    decoded_params.get('amountInMax', 0))),
                    'amountOutMin': decoded_params.get('amountOutMin',
                                decoded_params.get('amount1',
                                    decoded_params.get('amountOut', 0))),
                    'path': decoded_params.get('path', [])
                }

            # Validate parameters presence and format
            if not self._validate_params_format(params, function_name):
                logger.debug(f"Invalid parameter format for {function_name}")
                return None

            return params

        except Exception as e:
            logger.error(f"Error extracting transaction parameters: {e}")
            return None

    def _validate_params_format(self, params: Dict[str, Any], function_name: str) -> bool:
        """
        Validate parameter format based on function type.
        """
        try:
            if function_name in ['transfer', 'transferFrom']:
                required = ['amount', 'to']
                if function_name == 'transferFrom':
                    required.append('from')
            elif function_name in ['swap', 'swapExactTokensForTokens', 'swapTokensForExactTokens']:
                required = ['amountIn', 'amountOutMin', 'path']
            else:
                logger.debug(f"Unsupported function name: {function_name}")
                return False

            # Check required fields are present and non-empty
            for field in required:
                if params.get(field) is None or params.get(field) == '':
                    logger.debug(f"Missing or empty field '{field}' for function '{function_name}'")
                    return False

            return True

        except Exception as e:
            logger.error(f"Parameter validation error: {e}")
            return False

    async def stop(self) -> None:
        """
        Gracefully stop the Mempool Monitor.

        This method performs the following steps to ensure a clean shutdown:
        - Sets the `running` attribute to False to signal all running tasks to stop.
        - Sets the `stopping` attribute to True to indicate the stopping process has begun.
        - Waits for all tasks in the `task_queue` to complete using `task_queue.join()`.
        - Logs a debug message indicating the monitor has stopped gracefully.

        Raises:
            Exception: If an error occurs during the stopping process, it is logged and re-raised.
        """
        try:
            self.running = False
            self.stopping = True
            await self.task_queue.join()
            if self.session and not self.session.closed:
                await self.session.close()
            logger.debug("Mempool Monitor stopped gracefully.")

        except Exception as e:
            logger.error(f"Error stopping Mempool Monitor: {e}")
            raise
