pragma solidity ^0.8.20;

// SPDX-License-Identifier: MIT

import "https://github.com/aave/aave-v3-core/blob/master/contracts/flashloan/base/FlashLoanSimpleReceiverBase.sol";
import "https://github.com/aave/aave-v3-core/blob/master/contracts/interfaces/IPoolAddressesProvider.sol";
import "https://github.com/aave/aave-v3-core/blob/master/contracts/dependencies/openzeppelin/contracts/IERC20.sol";

interface IGasPriceOracle {
    function latestAnswer() external view returns (int256);
}

contract SimpleFlashLoan is FlashLoanSimpleReceiverBase {  // Removed "abstract"
    address payable public owner;
    IGasPriceOracle public gasPriceOracle;

    // Events
    event FlashLoanRequested(address indexed token, uint256 amount);
    event FlashLoanExecuted(address indexed token, uint256 amount, uint256 premium, bool success, string reason);
    event TokensWithdrawn(address indexed token, uint256 amount);
    event ETHWithdrawn(uint256 amount);

    // Errors
    error NotOwner();
    error ZeroAddress();
    error NoTokensToWithdraw();
    error TransferError();
    error ApprovalFailed();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address _addressProvider, address _gasPriceOracle) FlashLoanSimpleReceiverBase(IPoolAddressesProvider(_addressProvider)) {
        owner = payable(msg.sender); // Contract deployer becomes the owner
        gasPriceOracle = IGasPriceOracle(_gasPriceOracle);
    }


    function fn_RequestFlashLoan(address _token, uint256 _amount) external onlyOwner {
        if (_token == address(0)) revert ZeroAddress();
        emit FlashLoanRequested(_token, _amount);
        POOL.flashLoanSimple(address(this), _token, _amount, "", 0);
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address,  // initiator (unused)
        bytes calldata  // params (unused)
    ) external override returns (bool) {
        if (!IERC20(asset).approve(address(POOL), amount + premium)) revert ApprovalFailed();
        emit FlashLoanExecuted(asset, amount, premium, true, "Success");
        return true;
    }

    function withdrawToken(address _tokenAddress) external onlyOwner {
        if (_tokenAddress == address(0)) revert ZeroAddress();
        IERC20 token = IERC20(_tokenAddress);
        uint256 balance = token.balanceOf(address(this));
        if (balance == 0) revert NoTokensToWithdraw();
        if (!token.transfer(owner, balance)) revert TransferError();
        emit TokensWithdrawn(_tokenAddress, balance);
    }

    function withdrawETH() external onlyOwner {
        uint256 balance = address(this).balance;
        if (balance == 0) revert NoTokensToWithdraw();
        (bool success, ) = owner.call{value: balance}("");
        if (!success) revert TransferError();
        emit ETHWithdrawn(balance);
    }

    receive() external payable {}
}