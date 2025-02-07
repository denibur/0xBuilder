#========================================================================================================================
# File: transaction_core.py
#========================================================================================================================
import asyncio
import aiohttp
import hexbytes

from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound, ContractLogicError
from eth_account import Account
from typing import Any, Dict, List, Optional
from decimal import Decimal

from abi_registry import ABI_Registry
from api_config import API_Config
from configuration import Configuration
from market_monitor import Market_Monitor
from mempool_monitor import Mempool_Monitor
from nonce_core import Nonce_Core
from safety_net import Safety_Net
import logging as logger

logger = logger.getLogger(__name__)

class Transaction_Core:
    """
    Transaction_Core is the main transaction engine that handles all transaction-related
    Builds and executes transactions, including front-run, back-run, and sandwich attack strategies.
    It interacts with smart contracts, manages transaction signing, gas price estimation, and handles flashloans
    """
    DEFAULT_GAS_LIMIT: int = 100_000  # Default gas limit
    DEFAULT_PROFIT_TRANSFER_MULTIPLIER: int = 10**18
    DEFAULT_GAS_PRICE_GWEI: int = 50

    def __init__(
        self,
        web3: AsyncWeb3,
        account: Account,
        AAVE_FLASHLOAN_ABI: str,
        AAVE_POOL_ABI: str,
        AAVE_FLASHLOAN_ADDRESS: str,
        AAVE_POOL_ADDRESS: str,
        api_config: Optional["API_Config"] = None,
        market_monitor: Optional["Market_Monitor"] = None,
        mempool_monitor: Optional["Mempool_Monitor"] = None,
        nonce_core: Optional["Nonce_Core"] = None,
        safety_net: Optional["Safety_Net"] = None,
        configuration: Optional["Configuration"] = None,
        gas_price_multiplier: float = 1.1,
        erc20_abi: Optional[List[Dict[str, Any]]] = None,
        uniswap_address: Optional[str] = None,
        uniswap_abi: Optional[List[Dict[str, Any]]] = None
    ):
        """Initialize the Transaction Core."""
        self.web3: AsyncWeb3 = web3
        self.account: Account = account
        self.configuration: Optional["Configuration"] = configuration
        self.market_monitor: Optional["Market_Monitor"] = market_monitor
        self.mempool_monitor: Optional["Mempool_Monitor"] = mempool_monitor
        self.api_config: Optional["API_Config"] = api_config
        self.nonce_core: Optional["Nonce_Core"] = nonce_core
        self.safety_net: Optional["Safety_Net"] = safety_net
        self.gas_price_multiplier = gas_price_multiplier
        self.RETRY_ATTEMPTS: int = configuration.MEMPOOL_MAX_RETRIES
        self.erc20_abi: List[Dict[str, Any]] = erc20_abi or []
        self.current_profit: Decimal = Decimal("0")
        self.AAVE_FLASHLOAN_ADDRESS: str = AAVE_FLASHLOAN_ADDRESS
        self.AAVE_POOL_ADDRESS: str = AAVE_POOL_ADDRESS
        self.abi_registry: "ABI_Registry" = ABI_Registry()
        self.uniswap_address: str = uniswap_address
        self.uniswap_abi: List[Dict[str, Any]] = uniswap_abi or []

    def normalize_address(self, address: str) -> str:
        """Normalize Ethereum address to checksum format."""
        try:
            return self.web3.to_checksum_address(address)
        except Exception as e:
            self.handle_error(e, "normalize_address", {"address": address})
            raise

    async def __aenter__(self) -> "Transaction_Core":
        """Context manager entry point."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit point."""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.debug("TransactionCore session closed.")

    async def initialize(self) -> None:
        """Initialize with proper ABI loading."""
        try:
            abi_registry = ABI_Registry()
            await abi_registry.initialize(self.configuration.BASE_PATH)

            self.aave_flashloan = self.web3.eth.contract(
                address=self.normalize_address(self.AAVE_FLASHLOAN_ADDRESS),
                abi=abi_registry.get_abi('aave_flashloan')
            )
            await self._validate_contract(self.aave_flashloan, "Aave Flashloan", 'aave_flashloan')

            self.aave_pool = self.web3.eth.contract(
                address=self.normalize_address(self.AAVE_POOL_ADDRESS),
                abi=abi_registry.get_abi('aave')
            )
            await self._validate_contract(self.aave_pool, "Aave Lending Pool", 'aave')

            logger.info("Transaction Core initialized successfully")

        except Exception as e:
            self.handle_error(e, "initialize")
            raise

    async def _validate_contract(self, contract: Any, name: str, abi_type: str) -> None:
        """Validates contracts using a shared pattern."""
        try:
            if 'Lending Pool' in name:
                try:
                    await contract.functions.getReservesList().call()
                    logger.debug(f"{name} contract validated via getReservesList()")
                except (ContractLogicError, OverflowError):
                    await contract.functions.admin().call()
                    logger.debug(f"{name} contract validated via admin()")
            elif 'Flashloan' in name:
                try:
                    await contract.functions.ADDRESSES_PROVIDER().call()
                    logger.debug(f"{name} contract validated via ADDRESSES_PROVIDER()")
                except (ContractLogicError, OverflowError):
                    try:
                        await contract.functions.owner().call()
                        logger.debug(f"{name} contract validated via owner()")
                    except (ContractLogicError, OverflowError):
                        code = await self.web3.eth.get_code(contract.address)
                        if code and code != '0x':
                            logger.debug(f"{name} contract validated via code existence")
                        else:
                            raise ValueError(f"No code at {contract.address}")
            elif abi_type in ['uniswap', 'sushiswap']:
                try:
                    path = [self.configuration.WETH_ADDRESS, self.configuration.USDC_ADDRESS]
                    await contract.functions.getAmountsOut(1000000, path).call()
                    logger.debug(f"{name} contract validated via getAmountsOut()")
                except (ContractLogicError, OverflowError):
                    await contract.functions.factory().call()
                    logger.debug(f"{name} contract validated via factory()")
            else:
                code = await self.web3.eth.get_code(contract.address)
                if code and code != '0x':
                    logger.debug(f"{name} contract validated via code existence")
                else:
                    raise ValueError(f"No code at {contract.address}")

        except Exception as e:
            self.handle_error(e, "_validate_contract", {"contract": contract, "name": name, "abi_type": abi_type})
            logger.warning(f"Contract validation warning for {name}: {e}")

    async def build_transaction(self, function_call: Any, additional_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build transaction with EIP-1559 support and gas estimation."""
        additional_params = additional_params or {}
        try:
            chain_id = await self.web3.eth.chain_id
            latest_block = await self.web3.eth.get_block('latest')
            supports_eip1559 = 'baseFeePerGas' in latest_block

            tx_params = {
                'chainId': chain_id,
                'nonce': await self.nonce_core.get_nonce(),
                'from': self.account.address,
            }

            if supports_eip1559:
                base_fee = latest_block['baseFeePerGas']
                priority_fee = await self.web3.eth.max_priority_fee

                tx_params.update({
                    'maxFeePerGas': int(base_fee * 2),
                    'maxPriorityFeePerGas': int(priority_fee)
                })
            else:
                tx_params.update(await self._get_dynamic_gas_parameters())

            tx_details = function_call.buildTransaction(tx_params)
            tx_details.update(additional_params)

            estimated_gas = await self.estimate_gas_smart(tx_details)
            tx_details['gas'] = int(estimated_gas * 1.1)
            return tx_details

        except Exception as e:
            self.handle_error(e, "build_transaction", {"function_call": function_call, "additional_params": additional_params})
            raise

    async def _get_dynamic_gas_parameters(self) -> Dict[str, int]:
        """Gets dynamic gas price adjusted by the multiplier."""
        try:
             gas_price_gwei = await self.safety_net.get_dynamic_gas_price()
             logger.debug(f"Fetched gas price: {gas_price_gwei} Gwei")
        except Exception as e:
            self.handle_error(e, "_get_dynamic_gas_parameters")
            gas_price_gwei = Decimal(self.DEFAULT_GAS_PRICE_GWEI)

        gas_price = int(
            self.web3.to_wei(gas_price_gwei * self.gas_price_multiplier, "gwei")
        )
        return {"gasPrice": gas_price}

    async def estimate_gas_smart(self, tx: Dict[str, Any]) -> int:
        """Estimates gas with fallback to a default value."""
        try:
            gas_estimate = await self.web3.eth.estimate_gas(tx)
            logger.debug(f"Estimated gas: {gas_estimate}")
            return gas_estimate
        except ContractLogicError as e:
            self.handle_error(e, "estimate_gas_smart", {"tx": tx})
            return self.DEFAULT_GAS_LIMIT
        except TransactionNotFound as e:
            self.handle_error(e, "estimate_gas_smart", {"tx": tx})
            return self.DEFAULT_GAS_LIMIT
        except Exception as e:
            self.handle_error(e, "estimate_gas_smart", {"tx": tx})
            return self.DEFAULT_GAS_LIMIT

    async def execute_transaction(self, tx: Dict[str, Any]) -> Optional[str]:
        """Executes a transaction with retries."""
        try:
            for attempt in range(1, self.configuration.MEMPOOL_MAX_RETRIES + 1):
                signed_tx = await self.sign_transaction(tx)
                tx_hash = await self.call_contract_function(signed_tx)
                logger.debug(f"Transaction sent successfully: {tx_hash.hex()}")
                return tx_hash.hex()
        except TransactionNotFound as e:
            self.handle_error(e, "execute_transaction", {"tx": tx})
        except ContractLogicError as e:
            self.handle_error(e, "execute_transaction", {"tx": tx})
        except Exception as e:
            self.handle_error(e, "execute_transaction", {"tx": tx})
            await asyncio.sleep(self.configuration.MEMPOOL_RETRY_DELAY * (attempt + 1))
        logger.error("Failed to execute transaction after retries")
        return None

    async def sign_transaction(self, transaction: Dict[str, Any]) -> bytes:
        """Signs a transaction with the account's private key."""
        try:
            signed_tx = self.web3.eth.account.sign_transaction(
                transaction,
                private_key=self.account.key,
            )
            logger.debug(
                f"Transaction signed successfully: Nonce {transaction['nonce']}. ✍️ "
            )
            return signed_tx.rawTransaction
        except KeyError as e:
            self.handle_error(e, "sign_transaction", {"transaction": transaction})
            raise
        except Exception as e:
            self.handle_error(e, "sign_transaction", {"transaction": transaction})
            raise

    async def handle_eth_transaction(self, target_tx: Dict[str, Any]) -> bool:
        """Handles an ETH transfer transaction."""
        tx_hash = target_tx.get("tx_hash", "Unknown")
        logger.debug(f"Handling ETH transaction {tx_hash}")
        try:
            eth_value = target_tx.get("value", 0)
            if eth_value <= 0:
                logger.debug("Transaction value is zero or negative. Skipping.")
                return False

            tx_details = {
                "to": target_tx.get("to", ""),
                "value": eth_value,
                "gas": 21_000,
                "nonce": await self.nonce_core.get_nonce(),
                "chainId": self.web3.eth.chain_id,
                "from": self.account.address,
            }
            original_gas_price = int(target_tx.get("gasPrice", 0))
            if original_gas_price <= 0:
                logger.warning("Original gas price is zero or negative. Skipping.")
                return False
            tx_details["gasPrice"] = int(
                original_gas_price * self.configuration.ETH_TX_GAS_PRICE_MULTIPLIER
            )

            eth_value_ether = self.web3.from_wei(eth_value, "ether")
            logger.debug(
                f"Building ETH front-run transaction for {eth_value_ether} ETH to {tx_details['to']}"
            )
            tx_hash_executed = await self.execute_transaction(tx_details)
            if tx_hash_executed:
                logger.debug(
                    f"Successfully executed ETH transaction with hash: {tx_hash_executed} ✅ "
                )
                return True
            else:
                logger.warning("Failed to execute ETH transaction. Retrying... ⚠️ ")

        except Exception as e:
            self.handle_error(e, "handle_eth_transaction", {"target_tx": target_tx})
        return False

    def calculate_flashloan_amount(self, target_tx: Dict[str, Any]) -> int:
        """Calculates the flashloan amount based on estimated profit."""
        estimated_profit = target_tx.get("profit", 0)
        if estimated_profit > 0:
            flashloan_amount = int(
                Decimal(estimated_profit) * Decimal(str(self.configuration.FLASHLOAN_BACK_RUN_PROFIT_PERCENTAGE)) * Decimal("1e18")
            )
            logger.debug(
                f"Calculated flashloan amount: {flashloan_amount} Wei based on estimated profit."
            )
            return flashloan_amount
        else:
            logger.debug("No estimated profit. Setting flashloan amount to 0.")
            return 0

    async def simulate_transaction(self, transaction: Dict[str, Any]) -> bool:
        """Simulates a transaction to check if it will succeed."""
        logger.debug(
            f"Simulating transaction with nonce {transaction.get('nonce', 'Unknown')}."
        )
        try:
            await self.web3.eth.call(transaction, block_identifier="pending")
            logger.debug("Transaction simulation succeeded.")
            return True
        except ContractLogicError as e:
            self.handle_error(e, "simulate_transaction", {"transaction": transaction})
            return False
        except Exception as e:
            self.handle_error(e, "simulate_transaction", {"transaction": transaction})
            return False

    async def prepare_flashloan_transaction(
        self, flashloan_asset: str, flashloan_amount: int
    ) -> Optional[Dict[str, Any]]:
        """Prepares a flashloan transaction."""
        if flashloan_amount <= 0:
            logger.debug(
                "Flashloan amount is 0 or less, skipping flashloan transaction preparation."
            )
            return None
        try:
            function_call = self.aave_flashloan.functions.fn_RequestFlashLoan(
                flashloan_asset,
                flashloan_amount
            )
            tx = await self.build_transaction(function_call)
            return tx
        except ContractLogicError as e:
            self.handle_error(e, "prepare_flashloan_transaction", {"flashloan_asset": flashloan_asset, "flashloan_amount": flashloan_amount})
            return None
        except Exception as e:
            self.handle_error(e, "prepare_flashloan_transaction", {"flashloan_asset": flashloan_asset, "flashloan_amount": flashloan_amount})
            return None

    async def send_bundle(self, transactions: List[Dict[str, Any]]) -> bool:
        """Sends a bundle of transactions to MEV relays."""
        try:
            signed_txs = [await self.sign_transaction(tx) for tx in transactions]
            bundle_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_sendBundle",
                "params": [
                    {
                        "txs": [signed_tx.hex() for signed_tx in signed_txs],
                        "blockNumber": hex(await self.web3.eth.block_number + 1),
                    }
                ],
            }

            mev_builders = self.configuration.MEV_BUILDERS # Use configurable MEV builders

            successes = []

            for builder in mev_builders:
                headers = {
                    "Content-Type": "application/json",
                    builder["auth_header"]: f"{self.account.address}:{self.account.key}",
                }

                for attempt in range(1, self.configuration.MEMPOOL_MAX_RETRIES + 1):
                    try:
                        logger.debug(f"Attempt {attempt} to send bundle via {builder['name']}. ℹ️ ")
                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                builder["url"],
                                json=bundle_payload,
                                headers=headers,
                                timeout=30,
                            ) as response:
                                response.raise_for_status()
                                response_data = await response.json()

                                if "error" in response_data:
                                    self.handle_error(ValueError(response_data["error"]), "send_bundle", {"transactions": transactions})
                                    raise ValueError(response_data["error"])

                                logger.info(f"Bundle sent successfully via {builder['name']}. ✅ ")
                                successes.append(builder['name'])
                                break

                    except aiohttp.ClientResponseError as e:
                        self.handle_error(e, "send_bundle", {"transactions": transactions})
                        if attempt < self.configuration.MEMPOOL_MAX_RETRIES:
                            sleep_time = self.configuration.MEMPOOL_RETRY_DELAY * attempt
                            logger.warning(f"Retrying in {sleep_time} seconds...")
                            await asyncio.sleep(sleep_time)
                    except ValueError as e:
                        self.handle_error(e, "send_bundle", {"transactions": transactions})
                        break
                    except Exception as e:
                        self.handle_error(e, "send_bundle", {"transactions": transactions})
                        if attempt < self.configuration.MEMPOOL_MAX_RETRIES:
                            sleep_time = self.configuration.MEMPOOL_RETRY_DELAY * attempt
                            logger.warning(f"Retrying in {sleep_time} seconds...")
                            await asyncio.sleep(sleep_time)

            if successes:
                await self.nonce_core.refresh_nonce()
                logger.info(f"Bundle successfully sent to builders: {', '.join(successes)} ✅ ")
                return True
            else:
                logger.warning("Failed to send bundle to any MEV builders. ⚠️ ")
                return False

        except Exception as e:
            self.handle_error(e, "send_bundle", {"transactions": transactions})
            return False

    async def _validate_transaction(self, tx: Dict[str, Any], operation: str) -> Optional[Dict[str, Any]]:
        """Common transaction validation logic."""
        if not isinstance(tx, dict):
            logger.debug("Invalid transaction format provided!")
            return None

        required_fields = ["input", "to", "value", "gasPrice"]
        if not all(field in tx for field in required_fields):
            missing = [field for field in required_fields if field not in tx]
            logger.debug(f"Missing required parameters for {operation}: {missing}")
            return None

        decoded_tx = await self.decode_transaction_input(
            tx.get("input", "0x"),
            self.web3.to_checksum_address(tx.get("to", ""))
        )
        if not decoded_tx or "params" not in decoded_tx:
            logger.debug(f"Failed to decode transaction input for {operation}")
            return None

        path = decoded_tx["params"].get("path", [])
        if not path or not isinstance(path, list) or len(path) < 2:
            logger.debug(f"Invalid path parameter for {operation}")
            return None

        return decoded_tx

    async def front_run(self, target_tx: Dict[str, Any]) -> bool:
        """Execute front-run transaction with validation."""
        decoded_tx = await self._validate_transaction(target_tx, "front-run")
        if not decoded_tx:
            return False

        try:
            path = decoded_tx["params"]["path"]
            flashloan_tx = await self._prepare_flashloan(path[0], target_tx)
            front_run_tx = await self._prepare_front_run_transaction(target_tx)

            if not all([flashloan_tx, front_run_tx]):
                return False
            if await self._validate_and_send_bundle([flashloan_tx, front_run_tx]):
                logger.info("Front-run executed successfully ✅")
                return True
            return False
        except Exception as e:
            self.handle_error(e, "front_run", {"target_tx": target_tx})
            return False

    async def back_run(self, target_tx: Dict[str, Any]) -> bool:
        """Execute back-run transaction with validation."""
        decoded_tx = await self._validate_transaction(target_tx, "back-run")
        if not decoded_tx:
            return False

        try:
            back_run_tx = await self._prepare_back_run_transaction(target_tx, decoded_tx)
            if not back_run_tx:
                return False
            if await self._validate_and_send_bundle([back_run_tx]):
                logger.info("Back-run executed successfully ✅")
                return True
            return False
        except Exception as e:
            self.handle_error(e, "back_run", {"target_tx": target_tx})
            return False

    async def execute_sandwich_attack(self, target_tx: Dict[str, Any]) -> bool:
        """Execute sandwich attack with validation."""
        decoded_tx = await self._validate_transaction(target_tx, "sandwich")
        if not decoded_tx:
            return False

        try:
            path = decoded_tx["params"]["path"]
            flashloan_tx = await self._prepare_flashloan(path[0], target_tx)
            front_tx = await self._prepare_front_run_transaction(target_tx)
            back_tx = await self._prepare_back_run_transaction(target_tx, decoded_tx)

            if not all([flashloan_tx, front_tx, back_tx]):
                return False
            if await self._validate_and_send_bundle([flashloan_tx, front_tx, back_tx]):
                logger.info("Sandwich attack executed successfully 🥪✅")
                return True
            return False
        except Exception as e:
            self.handle_error(e, "execute_sandwich_attack", {"target_tx": target_tx})
            return False

    async def _prepare_flashloan(self, asset: str, target_tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Helper to prepare flashloan transaction."""
        flashloan_amount = self.calculate_flashloan_amount(target_tx)
        if flashloan_amount <= 0:
            return None
        return await self.prepare_flashloan_transaction(
            self.web3.to_checksum_address(asset),
            flashloan_amount
        )

    async def _validate_and_send_bundle(self, transactions: List[Dict[str, Any]]) -> bool:
        """Validate and send a bundle of transactions."""
        simulations = await asyncio.gather(
            *[self.simulate_transaction(tx) for tx in transactions],
            return_exceptions=True
        )

        if any(isinstance(result, Exception) or not result for result in simulations):
            logger.warning("Transaction simulation failed")
            return False

        return await self.send_bundle(transactions)

    async def _prepare_front_run_transaction(
        self, target_tx: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Prepares the front-run transaction based on the target transaction.
        :param target_tx: Target transaction dictionary.
        :return: Front-run transaction dictionary if successful, else None.
        """
        try:
            decoded_tx = await self.decode_transaction_input(
                target_tx.get("input", "0x"),
                self.web3.to_checksum_address(target_tx.get("to", ""))
            )
            if not decoded_tx:
                logger.debug("Failed to decode target transaction input for front-run. Skipping.")
                return None

            function_name = decoded_tx.get("function_name")
            if not function_name:
                logger.debug("Missing function name in decoded transaction.  🚨")
                return None

            function_params = decoded_tx.get("params", {})
            to_address = self.web3.to_checksum_address(target_tx.get("to", ""))

            # Router address mapping
            routers = {
                self.configuration.UNISWAP_ADDRESS: (getattr(self, "uniswap_router_contract"), "Uniswap"),
                self.configuration.SUSHISWAP_ADDRESS: (getattr(self, "sushiswap_router_contract"), "Sushiswap"),
            }
            if to_address not in routers:
                logger.warning(f"Unknown router address {to_address}. Cannot determine exchange.")
                return None

            router_contract, exchange_name = routers[to_address]
            if not router_contract:
                logger.warning(f"Router contract not initialized for {exchange_name}.")
                return None

           # Get the function object by name
            try:
                front_run_function = getattr(router_contract.functions, function_name)(**function_params)
            except AttributeError:
                logger.debug(f"Function {function_name} not found in {exchange_name} router ABI.")
                return None

            # Build the transaction
            front_run_tx = await self.build_transaction(front_run_function)
            logger.info(f"Prepared front-run transaction on {exchange_name} successfully. ✅ ")
            return front_run_tx
        except Exception as e:
            self.handle_error(e, "_prepare_front_run_transaction", {"target_tx": target_tx})
            return None

    async def _prepare_back_run_transaction(
        self, target_tx: Dict[str, Any], decoded_tx: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Prepare the back-run transaction based on the target transaction.
        :param target_tx: Target transaction dictionary.
        :param decoded_tx: Decoded target transaction dictionary.
        :return: Back-run transaction dictionary if successful, else None.
        """
        try:
            function_name = decoded_tx.get("function_name")
            if not function_name:
                logger.debug("Missing function name in decoded transaction.")
                return None

            function_params = decoded_tx.get("params", {})

            # Handle path parameter for back-run
            path = function_params.get("path", [])
            if not path or not isinstance(path, list) or len(path) < 2:
                logger.debug("Transaction has invalid or no path parameter for back-run.")
                return None

            # Reverse the path for back-run
            reversed_path = path[::-1]
            function_params["path"] = reversed_path

            to_address = self.web3.to_checksum_address(target_tx.get("to", ""))

            # Router address mapping
            routers = {
                self.configuration.UNISWAP_ADDRESS: (getattr(self, "uniswap_router_contract"), "Uniswap"),
                self.configuration.SUSHISWAP_ADDRESS: (getattr(self, "sushiswap_router_contract"), "Sushiswap"),
            }

            if to_address not in routers:
                logger.debug(f"Unknown router address {to_address}. Cannot determine exchange.")
                return None

            router_contract, exchange_name = routers[to_address]
            if not router_contract:
                logger.debug(f"Router contract not initialized for {exchange_name}.")
                return None

           # Get the function object by name
            try:
                back_run_function = getattr(router_contract.functions, function_name)(**function_params)
            except AttributeError:
                logger.debug(f"Function {function_name} not found in {exchange_name} router ABI.")
                return None

            # Build the transaction
            back_run_tx = await self.build_transaction(back_run_function)
            logger.info(f"Prepared back-run transaction on {exchange_name} successfully.")
            return back_run_tx

        except Exception as e:
            self.handle_error(e, "_prepare_back_run_transaction", {"target_tx": target_tx, "decoded_tx": decoded_tx})
            return None

    async def decode_transaction_input(self, input_data: str, contract_address: str) -> Optional[Dict[str, Any]]:
        """Decode transaction input using ABI registry."""
        try:
            # Get selector from input
            selector = input_data[:10][2:]

            # Get method name from registry
            method_name = self.abi_registry.get_method_selector(selector)
            if not method_name:
                logger.debug(f"Unknown method selector: {selector}")
                return None

            # Get appropriate ABI for decoding
            for abi_type, abi in self.abi_registry.abis.items():
                try:
                    contract = self.web3.eth.contract(
                    address=self.web3.to_checksum_address(contract_address),
                        abi=abi
                    )
                    func_obj, decoded_params = contract.decode_function_input(input_data)
                    return {
                        "function_name": method_name,
                        "params": decoded_params,
                        "signature": selector,
                        "abi_type": abi_type
                    }
                except Exception:
                    continue

            return None

        except Exception as e:
            self.handle_error(e, "decode_transaction_input", {"input_data": input_data, "contract_address": contract_address})
            return None

    async def cancel_transaction(self, nonce: int) -> bool:
        """Cancels a stuck transaction by sending a zero-value transaction."""
        cancel_tx = {
            "to": self.account.address,
            "value": 0,
            "gas": 21_000,
            "gasPrice": self.web3.to_wei(self.configuration.DEFAULT_CANCEL_GAS_PRICE_GWEI, "gwei"), # Use configurable cancel gas price
            "nonce": nonce,
            "chainId": await self.web3.eth.chain_id,
            "from": self.account.address,
        }

        try:
            signed_cancel_tx = await self.sign_transaction(cancel_tx)
            tx_hash = await self.web3.eth.send_raw_transaction(signed_cancel_tx)
            tx_hash_hex = (
                tx_hash.hex()
                if isinstance(tx_hash, hexbytes.HexBytes)
                else tx_hash
            )
            logger.debug(
                f"Cancellation transaction sent successfully: {tx_hash_hex}"
            )
            return True
        except Exception as e:
            self.handle_error(e, "cancel_transaction", {"nonce": nonce})
            return False

    async def estimate_gas_limit(self, tx: Dict[str, Any]) -> int:
        """Estimates the gas limit for a transaction."""
        try:
            gas_estimate = await self.web3.eth.estimate_gas(tx)
            logger.debug(f"Estimated gas: {gas_estimate}")
            return gas_estimate
        except Exception as e:
            self.handle_error(e, "estimate_gas_limit", {"tx": tx})
            return self.DEFAULT_GAS_LIMIT

    async def get_current_profit(self) -> Decimal:
        """Fetches the current profit from the safety net."""
        try:
            current_profit = await self.safety_net.get_balance(self.account)
            self.current_profit = Decimal(current_profit)
            logger.debug(f"Current profit: {self.current_profit} ETH")
            return self.current_profit
        except Exception as e:
            self.handle_error(e, "get_current_profit")
            return Decimal("0")

    async def withdraw_eth(self) -> bool:
        """Withdraws ETH from the flashloan contract."""
        try:
            withdraw_function = self.aave_flashloan.functions.withdrawETH()
            tx = await self.build_transaction(withdraw_function)
            tx_hash = await self.execute_transaction(tx)
            if (tx_hash):
                logger.debug(
                    f"ETH withdrawal transaction sent with hash: {tx_hash}"
                )
                return True
            else:
                logger.warning("Failed to send ETH withdrawal transaction.")
                return False
        except ContractLogicError as e:
            self.handle_error(e, "withdraw_eth")
            return False
        except Exception as e:
            self.handle_error(e, "withdraw_eth")
            return False

    async def transfer_profit_to_account(self, amount: Decimal, account: str) -> bool:
        """Transfers profit to another account."""
        try:
            transfer_function = self.aave_flashloan.functions.transfer(
                self.web3.to_checksum_address(account), int(amount * Decimal(self.DEFAULT_PROFIT_TRANSFER_MULTIPLIER))
            )
            tx = await self.build_transaction(transfer_function)
            tx_hash = await self.execute_transaction(tx)
            if tx_hash:
                logger.debug(
                    f"Profit transfer transaction sent with hash: {tx_hash}"
                )
                return True
            else:
                logger.warning("Failed to send profit transfer transaction.")
                return False
        except ContractLogicError as e:
            self.handle_error(e, "transfer_profit_to_account", {"amount": amount, "account": account})
            return False
        except Exception as e:
            self.handle_error(e, "transfer_profit_to_account", {"amount": amount, "account": account})
            return False

    async def stop(self) -> None:
        """Stop transaction core operations."""
        try:
            if self.session and not self.session.closed:
                await self.session.close()            
            await self.safety_net.stop()
            await self.nonce_core.stop()
            logger.debug("Stopped Transaction Core. ")
        except Exception as e:
            self.handle_error(e, "stop")
            raise

    async def calculate_gas_parameters(
        self,
        tx: Dict[str, Any],
        gas_limit: Optional[int] = None
    ) -> Dict[str, int]:
        """Centralized gas parameter calculation."""
        try:
            gas_params = await self._get_dynamic_gas_parameters()
            estimated_gas = gas_limit or await self.estimate_gas_smart(tx)
            return {
                'gasPrice': gas_params['gasPrice'],
                'gas': int(estimated_gas * 1.1)  # 10% buffer
            }
        except Exception as e:
            self.handle_error(e, "calculate_gas_parameters", {"tx": tx, "gas_limit": gas_limit})
            # Fallback to Default values
            return {
                "gasPrice": int(self.web3.to_wei(self.DEFAULT_GAS_PRICE_GWEI * self.gas_price_multiplier, "gwei")),
                "gas": self.DEFAULT_GAS_LIMIT
            }

    async def execute_transaction_with_gas_parameters(self, tx: Dict[str, Any], gas_params: Dict[str, int]) -> Optional[str]:
        """
        Executes a transaction with custom gas parameters.
        :param tx: Transaction dictionary.
        :return: Transaction hash if successful, else None.
        """
        try:
            tx.update(gas_params)
            tx_hash = await self.execute_transaction(tx)
            return tx_hash
        except Exception as e:
            self.handle_error(e, "execute_transaction_with_gas_parameters", {"tx": tx, "gas_params": gas_params})
            return None

    async def call_contract_function(self, signed_tx: bytes) -> hexbytes.HexBytes:
        """
        Call a contract function with a signed transaction.
        :param signed_tx: Signed transaction bytes.
        :return: Transaction hash.
        """
        try:
            tx_hash = await self.web3.eth.send_raw_transaction(signed_tx)
            return tx_hash
        except Exception as e:
            self.handle_error(e, "call_contract_function", {"signed_tx": signed_tx})
            raise

    def handle_error(self, error: Exception, function_name: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Centralized error handling function."""
        error_message = f"Error in {function_name}: {error}"
        if params:
            error_message += f" | Parameters: {params}"
        logger.error(error_message)

    async def _prepare_flashloan(self, asset: str, target_tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Helper to prepare flashloan transaction."""
        flashloan_amount = self.calculate_flashloan_amount(target_tx)
        if flashloan_amount <= 0:
            return None
        function_call = self.aave_flashloan.functions.fn_RequestFlashLoan(
            asset,
            flashloan_amount,
        )
        return await self.build_transaction(function_call)


    async def _prepare_front_run_transaction(self, target_tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Prepares front-run transaction."""
        decoded_tx = await self.decode_transaction_input(target_tx["input"], target_tx["to"])
        if not decoded_tx:
            logger.debug("Failed to decode target transaction input for front-run.")
            return None

        function_name = decoded_tx["function_name"]
        function_params = decoded_tx["params"]
        router_contract = self.uniswap_router_contract # Assuming Uniswap for front-run for now

        front_run_function = getattr(router_contract.functions, function_name)(**function_params)
        return await self.build_transaction(front_run_function)


    async def _prepare_back_run_transaction(self, target_tx: Dict[str, Any], decoded_tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Prepares back-run transaction."""
        function_name = decoded_tx["function_name"]
        function_params = decoded_tx["params"]
        router_contract = self.uniswap_router_contract # Assuming Uniswap for back-run for now

        path = function_params.get("path")
        if path:
            function_params["path"] = path[::-1]  # Reverse the path for back-run

        back_run_function = getattr(router_contract.functions, function_name)(**function_params)
        return await self.build_transaction(back_run_function)


    async def execute_sandwich_attack(self, target_tx: Dict[str, Any]) -> bool:
        """Executes a sandwich attack."""
        decoded_tx = await self.decode_transaction_input(target_tx["input"], target_tx["to"])
        if not decoded_tx:
            return False

        path = decoded_tx["params"]["path"]
        flashloan_tx = await self._prepare_flashloan(path[0], target_tx)
        front_run_tx = await self._prepare_front_run_transaction(target_tx)
        back_run_tx = await self._prepare_back_run_transaction(target_tx, decoded_tx)

        if not all([flashloan_tx, front_run_tx, back_run_tx]):
            logger.warning("Failed to prepare all sandwich components.")
            return False

        return await self._validate_and_send_bundle([flashloan_tx, front_run_tx, back_run_tx])