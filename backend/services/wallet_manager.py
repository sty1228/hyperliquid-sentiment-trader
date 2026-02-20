import os
import json
import logging
import requests
from eth_account import Account
from web3 import Web3
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# ── Config ──
ARB_RPC = "https://arb1.arbitrum.io/rpc"
USDC_ADDRESS = Web3.to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
HL_BRIDGE = Web3.to_checksum_address("0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7")

WALLET_ENCRYPTION_KEY = os.getenv("WALLET_ENCRYPTION_KEY", "")
BUILDER_ADDRESS = os.getenv("HL_BUILDER_ADDRESS", "")
BUILDER_FEE = int(os.getenv("HL_DEFAULT_BUILDER_BPS", "10"))

USDC_ABI = json.loads(
    '[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",'
    '"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],'
    '"name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},'
    '{"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],'
    '"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],'
    '"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]'
)


# ── Encryption ──

def get_fernet():
    if not WALLET_ENCRYPTION_KEY:
        raise ValueError("WALLET_ENCRYPTION_KEY not set in .env")
    return Fernet(WALLET_ENCRYPTION_KEY.encode())


def encrypt_key(private_key: str) -> str:
    return get_fernet().encrypt(private_key.encode()).decode()


def decrypt_key(encrypted: str) -> str:
    return get_fernet().decrypt(encrypted.encode()).decode()


# ── Wallet ──

def generate_wallet() -> dict:
    acct = Account.create()
    return {
        "address": acct.address,
        "private_key": acct.key.hex(),
    }


# ── Web3 ──

def get_web3() -> Web3:
    return Web3(Web3.HTTPProvider(ARB_RPC))


def get_usdc_balance(address: str) -> float:
    w3 = get_web3()
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    raw = usdc.functions.balanceOf(Web3.to_checksum_address(address)).call()
    return raw / 1e6


# ── Bridge ──

def bridge_usdc_to_hl(private_key: str, amount: float) -> str:
    w3 = get_web3()
    acct = Account.from_key(private_key)
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    amount_raw = int(amount * 1e6)

    # Approve if needed
    allowance = usdc.functions.allowance(acct.address, HL_BRIDGE).call()
    if allowance < amount_raw:
        approve_tx = usdc.functions.approve(
            HL_BRIDGE, 2**256 - 1
        ).build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 42161,
        })
        signed = acct.sign_transaction(approve_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        logger.info(f"Approved USDC spending for {acct.address}")

    # Transfer USDC to bridge
    transfer_tx = usdc.functions.transfer(
        HL_BRIDGE, amount_raw
    ).build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": 42161,
    })
    signed = acct.sign_transaction(transfer_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    logger.info(f"Bridged {amount} USDC for {acct.address}, tx: {receipt.transactionHash.hex()}")
    return receipt.transactionHash.hex()


# ── HyperLiquid Info ──

def get_hl_balance(address: str) -> dict:
    resp = requests.post("https://api.hyperliquid.xyz/info", json={
        "type": "clearinghouseState",
        "user": address.lower(),
    })
    data = resp.json()
    margin = data.get("marginSummary", {})
    return {
        "equity": float(margin.get("accountValue", "0")),
        "withdrawable": float(data.get("withdrawable", "0")),
        "positions": float(margin.get("totalNtlPos", "0")),
    }


# ── Withdraw from HL ──

def withdraw_from_hl(private_key: str, amount: float, destination: str) -> dict:
    from hyperliquid.exchange import Exchange
    import eth_account as eth_acc

    acct = eth_acc.Account.from_key(private_key)
    exchange = Exchange(wallet=acct, base_url="https://api.hyperliquid.xyz")
    result = exchange.withdraw(amount, destination)

    logger.info(f"HL withdraw {amount} USDC to {destination}: {result}")
    return result


# ── Transfer USDC back to user ──

def transfer_usdc_to_user(private_key: str, to_address: str, amount: float) -> str:
    w3 = get_web3()
    acct = Account.from_key(private_key)
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    amount_raw = int(amount * 1e6)

    tx = usdc.functions.transfer(
        Web3.to_checksum_address(to_address), amount_raw
    ).build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": 42161,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    logger.info(f"Transferred {amount} USDC to {to_address}, tx: {receipt.transactionHash.hex()}")
    return receipt.transactionHash.hex()


# ── Execute copy trade with builder code ──

def execute_copy_trade(
    private_key: str,
    coin: str,
    is_buy: bool,
    size: float,
    price: float,
    reduce_only: bool = False,
) -> dict:
    from hyperliquid.exchange import Exchange
    import eth_account as eth_acc

    acct = eth_acc.Account.from_key(private_key)
    exchange = Exchange(wallet=acct, base_url="https://api.hyperliquid.xyz")

    result = exchange.order(
        coin=coin,
        is_buy=is_buy,
        sz=size,
        limit_px=price,
        order_type={"limit": {"tif": "Ioc"}},
        reduce_only=reduce_only,
        builder={"b": BUILDER_ADDRESS, "f": BUILDER_FEE},
    )

    logger.info(f"Trade: {coin} {'BUY' if is_buy else 'SELL'} {size} @ {price}: {result}")
    return result


# ── Gas Station ──

GAS_STATION_KEY = os.getenv("GAS_STATION_KEY", "")
GAS_STATION_ADDRESS = os.getenv("GAS_STATION_ADDRESS", "")
MIN_GAS_ETH = 0.0003  # ~enough for approve+transfer
GAS_TOP_UP = 0.0008    # send this much when low


def get_eth_balance(address: str) -> float:
    w3 = get_web3()
    return w3.eth.get_balance(Web3.to_checksum_address(address)) / 1e18


def ensure_gas(wallet_address: str) -> bool:
    """Check if wallet has enough ETH for gas; if not, send from master wallet."""
    eth_bal = get_eth_balance(wallet_address)
    if eth_bal >= MIN_GAS_ETH:
        logger.info(f"[{wallet_address[:10]}...] ETH OK: {eth_bal:.6f}")
        return True

    if not GAS_STATION_KEY:
        logger.error("GAS_STATION_KEY not set — cannot fund gas")
        return False

    try:
        w3 = get_web3()
        master = Account.from_key(GAS_STATION_KEY)
        tx = {
            "from": master.address,
            "to": Web3.to_checksum_address(wallet_address),
            "value": w3.to_wei(GAS_TOP_UP, "ether"),
            "nonce": w3.eth.get_transaction_count(master.address),
            "gas": 21000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 42161,
        }
        signed = master.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        logger.info(f"[{wallet_address[:10]}...] Funded {GAS_TOP_UP} ETH, tx: {tx_hash.hex()}")
        return True
    except Exception as e:
        logger.error(f"[{wallet_address[:10]}...] Gas funding failed: {e}")
        return False