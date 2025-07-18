import logging
from typing import Dict, List, Any

from eth_typing import ChecksumAddress
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from typing_extensions import Unpack
from web3.types import TxParams

from gte_py.api.chain.clob_client import CLOBClient
from gte_py.api.chain.clob_manager import ICLOBManager
from gte_py.api.chain.clob_factory import CLOBFactory
from gte_py.api.chain.structs import OperatorRole
from gte_py.api.rest import RestApi
from gte_py.api.chain.token_client import TokenClient
from gte_py.api.rest.models import trade_to_model
from gte_py.configs import NetworkConfig
from gte_py.models import Market, Order, Trade, OrderSide, OrderType, OrderStatus, TimeInForce

logger = logging.getLogger(__name__)


class UserClient:
    def __init__(
            self,
            config: NetworkConfig,
            account: ChecksumAddress, clob: CLOBClient, token: TokenClient, rest: RestApi
    ):
        """
        Initialize the account client.

        Args:
            config: Network configuration
            account: EVM address of the account
            clob: CLOBClient instance
            token: TokenClient instance
            rest: RestApi instance for API interactions
        """
        self._config = config
        self._account = account
        self._clob = clob
        self._web3 = clob._web3
        self._token = token
        self._rest = rest

        # Initialize CLOB Manager
        self._clob_manager = ICLOBManager(web3=self._web3, contract_address=config.clob_manager_address)

    def get_clob_factory(self) -> CLOBFactory:
        if self._clob.clob_factory is None:
            raise RuntimeError("CLOBFactory is not initialized. Did you forget to call await CLOBClient.init()?")
        return self._clob.clob_factory

    async def get_eth_balance(self) -> int:
        """
        Get the user's ETH balance.

        Returns:
            User's ETH balance in wei
        """
        return await self._web3.eth.get_balance(self._account)

    async def wrap_eth(
            self, weth_address: ChecksumAddress, amount: int, **kwargs: Unpack[TxParams]
    ):
        return await self._token.get_weth(weth_address).deposit_eth(amount, **kwargs).send_wait()

    async def unwrap_eth(
            self, weth_address: ChecksumAddress, amount: int, **kwargs: Unpack[TxParams]
    ):
        return await self._token.get_weth(weth_address).withdraw_eth(amount, **kwargs).send_wait()

    async def deposit(
            self, token_address: ChecksumAddress, amount: int, **kwargs: Unpack[TxParams]
    ):
        """
        Deposit tokens to the exchange for trading.

        Args:
            token_address: Address of token to deposit
            amount: Amount to deposit
            **kwargs: Additional transaction parameters

        Returns:
            List of TypedContractFunction objects (approve and deposit)
        """

        clob_factory = self.get_clob_factory()

        token = self._token.get_erc20(token_address)
        if token_address == self._config.weth_address:
            weth_token = await token.balance_of(self._account)
            if weth_token < amount:
                wrap_amount = amount - weth_token
                logger.info("Not enough WETH in the account: asked for %d, got %d, lacking %d", amount, weth_token,
                            wrap_amount)
                await self.wrap_eth(
                    weth_address=token_address,
                    amount=wrap_amount,
                    **kwargs
                )
        allowance = await token.allowance(owner=self._account, spender=self._clob.get_factory_address())
        if allowance < amount:
            # approve the factory to spend tokens
            await token.approve(
                spender=self._clob.get_factory_address(), amount=amount, **kwargs
            ).send_wait()

        # Then deposit the tokens
        await clob_factory.deposit(
            account=self._account,
            token=token_address,
            amount=amount,
            from_operator=False,
            **kwargs,
        ).send_wait()

    async def withdraw(
            self, token_address: ChecksumAddress, amount: int, **kwargs: Unpack[TxParams]
    ):
        """
        Withdraw tokens from the exchange.

        Args:
            token_address: Address of token to withdraw
            amount: Amount to withdraw
            **kwargs: Additional transaction parameters

        Returns:
            TypedContractFunction for the withdrawal transaction
        """

        clob_factory = self.get_clob_factory()

        # Withdraw the tokens
        return await clob_factory.withdraw(
            account=self._account, token=token_address, amount=amount, to_operator=False, **kwargs
        ).send_wait()

    async def get_portfolio(self) -> Dict[str, Any]:
        """
        Get the user's portfolio including token balances and USD values.

        Returns:
            Dict containing portfolio information with token balances and total USD value
        """
        return await self._rest.get_user_portfolio(self._account)

    async def get_token_balances(self) -> List[Dict[str, Any]]:
        """
        Get the user's token balances with USD values.

        Returns:
            List of token balances with associated information
        """
        portfolio = await self.get_portfolio()
        return portfolio.get("tokens", [])

    async def get_total_usd_balance(self) -> float:
        """
        Get the user's total portfolio value in USD.

        Returns:
            Total portfolio value in USD
        """
        portfolio = await self.get_portfolio()
        return float(portfolio.get("totalUsdBalance", 0))

    async def get_lp_positions(self) -> dict:
        """
        Get the user's liquidity provider positions.

        Returns:
            List of liquidity provider positions
        """
        return await self._rest.get_user_lp_positions(self._account)

    async def get_token_balance(self, token_address: ChecksumAddress) -> int:
        """
        Get the user's balance for a specific token both on-chain and in the exchange.

        Args:
            token_address: Address of the token to check

        Returns:
            Tuple of (wallet_balance, exchange_balance) in human-readable format
        """

        clob_factory = self.get_clob_factory()
        exchange_balance_raw = await clob_factory.get_account_balance(
            self._account, token_address
        )

        return exchange_balance_raw

    def _encode_rules(self, roles: list[OperatorRole]) -> int:
        roles_int = 0
        for role in roles:
            roles_int |= role.value
        return roles_int

    async def approve_operator(self, operator_address: ChecksumAddress,
                               roles: list[OperatorRole] = [],
                               unsafe_withdraw: bool = False,
                               unsafe_launchpad_fill: bool = False,
                               **kwargs: Unpack[TxParams]):
        """
        Approve an operator to act on behalf of the account.

        Args:
            operator_address: Address of the operator to approve
            roles: List of roles to assign to the operator
            unsafe_withdraw: Whether to allow unsafe withdrawals
            unsafe_launchpad_fill: Whether to allow unsafe launchpad fills
            **kwargs: Additional transaction parameters

        Returns:
            Transaction result from the approve_operator operation
        """
        if OperatorRole.WITHDRAW in roles and not unsafe_withdraw:
            raise ValueError("Unsafe withdraw must be enabled to approve withdraw role")
        if OperatorRole.LAUNCHPAD_FILL in roles and not unsafe_launchpad_fill:
            raise ValueError("Unsafe launchpad fill must be enabled to approve launchpad fill role")
        roles_int = self._encode_rules(roles)
        logger.info(f"Approving operator {operator_address} for account {self._account} with roles {roles}")

        return await self._clob_manager.approve_operator(
            operator=operator_address,
            roles=roles_int,
            **kwargs
        ).send_wait()

    async def disapprove_operator(self, operator_address: ChecksumAddress,
                                  roles: list[OperatorRole],
                                  **kwargs: Unpack[TxParams]):
        """
        Disapprove an operator from acting on behalf of the account.

        Args:
            operator_address: Address of the operator to disapprove
            roles: List of roles to disapprove
            **kwargs: Additional transaction parameters

        Returns:
            Transaction result from the disapprove_operator operation
        """
        roles_int = self._encode_rules(roles)
        logger.info(f"Disapproving operator {operator_address} for account {self._account} with roles {roles}")
        return await self._clob_manager.disapprove_operator(
            operator=operator_address,
            roles=roles_int,
            **kwargs
        ).send_wait()

    async def is_operator_approved(self, operator_address: ChecksumAddress) -> bool:
        """
        Check if an operator is approved for the account.

        Args:
            operator_address: Address of the operator to check

        Returns:
            True if the operator is approved, False otherwise
        """
        return await self._clob_manager.approved_operators(
            account=self._account,
            operator=operator_address
        )

    async def get_trades(self, market: Market, limit: int = 100, offset: int = 0) -> List[Trade]:
        """
        Get trades for a specific market using the REST API.

        Args:
            market: Market to get trades from
            limit: Number of trades to retrieve (default 100)
            offset: Offset for pagination (default 0)

        Returns:
            List of Order objects representing trades
        """
        response = await self._rest.get_user_trades(self._account, market.address, limit=limit, offset=offset)
        return [trade_to_model(trade) for trade in response]

    async def get_open_orders(
            self, market: Market | None = None,
            limit: int = 100, offset: int = 0
    ) -> List[Order]:
        """
        Get open orders for an address on a specific market using the REST API.

        Args:
            market: EVM address of the market (optional, defaults to None)


        Returns:
            List of Order objects representing open orders
        """

        response = await self._rest.get_user_open_orders(self._account, market and market.address, limit=limit,
                                                         offset=offset)
        """Create an Order object from API response data"""

        return [
            Order(
                order_id=int(data['orderId']),
                market_address=to_checksum_address(data["marketAddress"]),
                side=OrderSide.from_str(data['side']),
                order_type=OrderType.LIMIT,
                remaining_amount=int(data['originalSize']) - int(data['sizeFilled']),
                original_amount=int(data['originalSize']),
                filled_amount=int(data['sizeFilled']),
                price=int(data['limitPrice']),
                time_in_force=TimeInForce.GTC,
                status=OrderStatus.OPEN,
                placed_at=int(data["placedAt"]),
            ) for data in response
        ]

    async def get_filled_orders(
            self, market: Market | None = None, limit: int = 100, offset: int = 0
    ) -> List[Order]:
        """
        Get filled orders for an address on a specific market using the REST API.

        Args:
            market: EVM address of the market (optional, defaults to None)

        Returns:
            List of Order objects representing filled orders
        """

        response = await self._rest.get_user_filled_orders(self._account, market and market.address, limit=limit,
                                                           offset=offset)
        return [
            Order(
                order_id=int(data['orderId']),
                market_address=to_checksum_address(data["marketAddress"]),
                side=OrderSide.from_str(data['side']),
                order_type=OrderType.LIMIT,
                filled_amount=int(data['sizeFilled']),
                price=int(data['price']),
                time_in_force=TimeInForce.GTC,
                status=OrderStatus.FILLED,
                filled_at=int(data["filledAt"]),
                txn_hash=HexBytes(data['txnHash']),
            ) for data in response
        ]

    async def get_order_history(
            self, market: Market | None = None, limit: int = 100, offset: int = 0
    ) -> List[Order]:
        """
        Get order history for an address on a specific market using the REST API.

        Args:
            market: EVM address of the market (optional, defaults to None)

        Returns:
            List of Order objects representing order history
        """

        response = await self._rest.get_user_order_history(self._account, market and market.address, limit=limit,
                                                           offset=offset)
        return [
            Order(
                order_id=int(data['orderId']),
                market_address=to_checksum_address(data["marketAddress"]),
                side=OrderSide.from_str(data['side']),
                order_type=OrderType.LIMIT,
                remaining_amount=int(data['originalSize']) - int(data['sizeFilled']),
                original_amount=int(data['originalSize']),
                price=int(data['limitPrice']),
                time_in_force=TimeInForce.GTC,
                status=OrderStatus.OPEN,
                placed_at=int(data["placedAt"]),
            ) for data in response
        ]
