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

# ── Master Wallet (gas station + USDC pool for withdrawals) ──

MASTER_WALLET_KEY = os.getenv("GAS_STATION_KEY", "")
MASTER_WALLET_ADDRESS = os.getenv("GAS_STATION_ADDRESS", "")

# ── Stargate V2 Outbound Config (from Arbitrum) ──

ARB_STARGATE_POOL_USDC = Web3.to_checksum_address(
    "0xe8CDF27AcD73a434D661C84887215F7598e7d0d3"
)

CHAIN_ID_TO_LZ_EID = {
    1:      30101,   # Ethereum
    10:     30111,   # Optimism
    137:    30109,   # Polygon
    8453:   30184,   # Base
    43114:  30106,   # Avalanche
    5000:   30181,   # Mantle
    534352: 30214,   # Scroll
}

STARGATE_POOL_ABI = json.loads("""[
  {
    "inputs":[
      {"components":[
        {"name":"dstEid","type":"uint32"},
        {"name":"to","type":"bytes32"},
        {"name":"amountLD","type":"uint256"},
        {"name":"minAmountLD","type":"uint256"},
        {"name":"extraOptions","type":"bytes"},
        {"name":"composeMsg","type":"bytes"},
        {"name":"oftCmd","type":"bytes"}
      ],"name":"_sendParam","type":"tuple"},
      {"name":"_payInLzToken","type":"bool"}
    ],
    "name":"quoteSend",
    "outputs":[
      {"components":[
        {"name":"nativeFee","type":"uint256"},
        {"name":"lzTokenFee","type":"uint256"}
      ],"name":"msgFee","type":"tuple"},
      {"components":[
        {"name":"amountSentLD","type":"uint256"},
        {"name":"amountReceivedLD","type":"uint256"}
      ],"name":"oftReceipt","type":"tuple"}
    ],
    "stateMutability":"view",
    "type":"function"
  },
  {
    "inputs":[
      {"components":[
        {"name":"dstEid","type":"uint32"},
        {"name":"to","type":"bytes32"},
        {"name":"amountLD","type":"uint256"},
        {"name":"minAmountLD","type":"uint256"},
        {"name":"extraOptions","type":"bytes"},
        {"name":"composeMsg","type":"bytes"},
        {"name":"oftCmd","type":"bytes"}
      ],"name":"_sendParam","type":"tuple"},
      {"components":[
        {"name":"nativeFee","type":"uint256"},
        {"name":"lzTokenFee","type":"uint256"}
      ],"name":"_fee","type":"tuple"},
      {"name":"_refundAddress","type":"address"}
    ],
    "name":"sendToken",
    "outputs":[
      {"components":[
        {"name":"guid","type":"bytes32"},
        {"name":"nonce","type":"uint64"},
        {"components":[
          {"name":"nativeFee","type":"uint256"},
          {"name":"lzTokenFee","type":"uint256"}
        ],"name":"fee","type":"tuple"}
      ],"name":"msgReceipt","type":"tuple"},
      {"components":[
        {"name":"amountSentLD","type":"uint256"},
        {"name":"amountReceivedLD","type":"uint256"}
      ],"name":"oftReceipt","type":"tuple"}
    ],
    "stateMutability":"payable",
    "type":"function"
  }
]""")


# ═══════════════════════════════════════════════════════
# Encryption
# ═══════════════════════════════════════════════════════

def get_fernet():
    if not WALLET_ENCRYPTION_KEY:
        raise ValueError("WALLET_ENCRYPTION_KEY not set in .env")
    return Fernet(WALLET_ENCRYPTION_KEY.encode())


def encrypt_key(private_key: str) -> str:
    return get_fernet().encrypt(private_key.encode()).decode()


def decrypt_key(encrypted: str) -> str:
    return get_fernet().decrypt(encrypted.encode()).decode()


# ═══════════════════════════════════════════════════════
# Wallet
# ═══════════════════════════════════════════════════════

def generate_wallet() -> dict:
    acct = Account.create()
    return {
        "address": acct.address,
        "private_key": acct.key.hex(),
    }


# ═══════════════════════════════════════════════════════
# Web3
# ═══════════════════════════════════════════════════════

def get_web3() -> Web3:
    return Web3(Web3.HTTPProvider(ARB_RPC))


def get_usdc_balance(address: str) -> float:
    w3 = get_web3()
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    raw = usdc.functions.balanceOf(Web3.to_checksum_address(address)).call()
    return raw / 1e6


# ═══════════════════════════════════════════════════════
# Gas Station
# ═══════════════════════════════════════════════════════

MIN_GAS_ETH = 0.0003
GAS_TOP_UP = 0.0003


def _get_eip1559_fees(w3):
    """Get EIP-1559 gas fees with buffer for Arbitrum."""
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
    max_fee = int(base_fee * 1.5) + w3.to_wei(0.1, "gwei")
    max_priority = w3.to_wei(0.01, "gwei")
    return max_fee, max_priority


def get_eth_balance(address: str) -> float:
    w3 = get_web3()
    return w3.eth.get_balance(Web3.to_checksum_address(address)) / 1e18


def ensure_gas(wallet_address: str, min_eth: float = MIN_GAS_ETH, top_up_eth: float = GAS_TOP_UP) -> bool:
    """Check if wallet has enough ETH for gas; if not, send from master wallet."""
    eth_bal = get_eth_balance(wallet_address)
    if eth_bal >= min_eth:
        logger.info(f"[{wallet_address[:10]}...] ETH OK: {eth_bal:.6f}")
        return True

    if not MASTER_WALLET_KEY:
        logger.error("GAS_STATION_KEY not set — cannot fund gas")
        return False

    try:
        w3 = get_web3()
        master = Account.from_key(MASTER_WALLET_KEY)
        max_fee, max_priority = _get_eip1559_fees(w3)
        tx = {
            "from": master.address,
            "to": Web3.to_checksum_address(wallet_address),
            "value": w3.to_wei(top_up_eth, "ether"),
            "nonce": w3.eth.get_transaction_count(master.address),
            "gas": 50000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
            "chainId": 42161,
            "type": 2,
        }
        signed = master.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        logger.info(f"[{wallet_address[:10]}...] Funded {top_up_eth} ETH, tx: {tx_hash.hex()}")
        return True
    except Exception as e:
        logger.error(f"[{wallet_address[:10]}...] Gas funding failed: {e}")
        return False


# ═══════════════════════════════════════════════════════
# Bridge USDC to HyperLiquid (deposit flow)
# ═══════════════════════════════════════════════════════

def bridge_usdc_to_hl(private_key: str, amount: float) -> str:
    w3 = get_web3()
    acct = Account.from_key(private_key)
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    amount_raw = int(amount * 1e6)

    max_fee, max_priority = _get_eip1559_fees(w3)

    allowance = usdc.functions.allowance(acct.address, HL_BRIDGE).call()
    if allowance < amount_raw:
        approve_tx = usdc.functions.approve(
            HL_BRIDGE, 2**256 - 1
        ).build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100_000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
            "chainId": 42161,
            "type": 2,
        })
        signed = acct.sign_transaction(approve_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        logger.info(f"Approved USDC spending for {acct.address}")

    transfer_tx = usdc.functions.transfer(
        HL_BRIDGE, amount_raw
    ).build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 100_000,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": max_priority,
        "chainId": 42161,
        "type": 2,
    })
    signed = acct.sign_transaction(transfer_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    logger.info(f"Bridged {amount} USDC for {acct.address}, tx: {receipt.transactionHash.hex()}")
    return receipt.transactionHash.hex()


# ═══════════════════════════════════════════════════════
# Stargate V2 Bridge Out (Arb → other chain)
# ═══════════════════════════════════════════════════════

def stargate_bridge_out(private_key: str, amount: float, dest_chain_id: int, dest_address: str) -> str:
    """Bridge USDC from Arbitrum to destination chain via Stargate V2."""
    lz_eid = CHAIN_ID_TO_LZ_EID.get(dest_chain_id)
    if not lz_eid:
        raise ValueError(f"Unsupported destination chain ID: {dest_chain_id}")

    w3 = get_web3()
    acct = Account.from_key(private_key)

    amount_raw = int(amount * 1e6)
    min_amount = int(amount_raw * 995 // 1000)

    addr_bytes = bytes.fromhex(dest_address[2:] if dest_address.startswith("0x") else dest_address)
    dest_bytes32 = b"\x00" * 12 + addr_bytes

    send_param = (
        lz_eid, dest_bytes32, amount_raw, min_amount,
        b"", b"", b"",  # extraOptions, composeMsg, oftCmd (taxi mode)
    )

    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    pool = w3.eth.contract(address=ARB_STARGATE_POOL_USDC, abi=STARGATE_POOL_ABI)
    max_fee, max_priority = _get_eip1559_fees(w3)

    # 1. Approve USDC to Stargate pool
    allowance = usdc.functions.allowance(acct.address, ARB_STARGATE_POOL_USDC).call()
    if allowance < amount_raw:
        approve_tx = usdc.functions.approve(
            ARB_STARGATE_POOL_USDC, 2**256 - 1
        ).build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100_000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
            "chainId": 42161,
            "type": 2,
        })
        signed = acct.sign_transaction(approve_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        logger.info(f"Approved USDC for Stargate pool: {acct.address}")

    # 2. Quote LZ messaging fee
    msg_fee, _ = pool.functions.quoteSend(send_param, False).call()
    native_fee = int(msg_fee[0] * 110 // 100)

    logger.info(
        f"Stargate quote: native_fee={native_fee} wei "
        f"({native_fee / 1e18:.6f} ETH) for chain {dest_chain_id}"
    )

    # 3. Send token
    nonce = w3.eth.get_transaction_count(acct.address)
    send_tx = pool.functions.sendToken(
        send_param, (native_fee, 0), acct.address
    ).build_transaction({
        "from": acct.address,
        "value": native_fee,
        "nonce": nonce,
        "gas": 500_000,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": max_priority,
        "chainId": 42161,
        "type": 2,
    })
    signed = acct.sign_transaction(send_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    logger.info(
        f"Stargate bridge-out {amount} USDC → chain {dest_chain_id} "
        f"({dest_address[:10]}...), tx: {receipt.transactionHash.hex()}"
    )
    return receipt.transactionHash.hex()


# ═══════════════════════════════════════════════════════
# HyperLiquid Info
# ═══════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════
# HL Internal Transfer (zero fee — usd_transfer / usdSend)
# ═══════════════════════════════════════════════════════

def hl_internal_transfer(private_key: str, amount: float, destination: str) -> dict:
    """Transfer USDC within HL L1 between wallets. No bridge fee."""
    from hyperliquid.exchange import Exchange
    import eth_account as eth_acc

    acct = eth_acc.Account.from_key(private_key)
    exchange = Exchange(wallet=acct, base_url="https://api.hyperliquid.xyz")
    result = exchange.usd_transfer(amount, destination)

    logger.info(f"HL internal transfer {amount} USDC → {destination[:10]}...: {result}")
    return result


# ═══════════════════════════════════════════════════════
# HL Builder Fee Approval (auto-signed by backend)
# ═══════════════════════════════════════════════════════

def approve_builder_fee_for_wallet(private_key: str) -> dict:
    """Auto-approve builder fee for a dedicated wallet on HL.
    Called after first deposit bridges to HL so the wallet is
    ready for copy trading without any user-facing approval step.
    Idempotent — safe to call multiple times."""
    from hyperliquid.exchange import Exchange
    import eth_account as eth_acc

    if not BUILDER_ADDRESS:
        logger.warning("HL_BUILDER_ADDRESS not set, skipping builder fee approval")
        return {"status": "skipped"}

    acct = eth_acc.Account.from_key(private_key)
    exchange = Exchange(wallet=acct, base_url="https://api.hyperliquid.xyz")
    result = exchange.approve_builder_fee(BUILDER_ADDRESS, f"{BUILDER_FEE / 100}%")

    logger.info(
        f"[{acct.address[:10]}...] Builder fee approved "
        f"(builder={BUILDER_ADDRESS[:10]}..., fee={BUILDER_FEE} bps): {result}"
    )
    return result


# ═══════════════════════════════════════════════════════
# Withdraw from HL via bridge ($1 fee — fallback only)
# ═══════════════════════════════════════════════════════

def withdraw_from_hl(private_key: str, amount: float, destination: str) -> dict:
    from hyperliquid.exchange import Exchange
    import eth_account as eth_acc

    acct = eth_acc.Account.from_key(private_key)
    exchange = Exchange(wallet=acct, base_url="https://api.hyperliquid.xyz")
    result = exchange.withdraw_from_bridge(amount, destination)

    logger.info(f"HL withdraw {amount} USDC to {destination}: {result}")
    return result


# ═══════════════════════════════════════════════════════
# Master Wallet Helpers
# ═══════════════════════════════════════════════════════

def get_master_arb_usdc_balance() -> float:
    """Check master wallet USDC balance on Arbitrum."""
    if not MASTER_WALLET_ADDRESS:
        return 0.0
    return get_usdc_balance(MASTER_WALLET_ADDRESS)


def master_transfer_usdc(to_address: str, amount: float) -> str:
    """Transfer USDC from master wallet on Arb to any address."""
    if not MASTER_WALLET_KEY:
        raise ValueError("GAS_STATION_KEY not set")
    return transfer_usdc_to_user(MASTER_WALLET_KEY, to_address, amount)


def master_withdraw_from_hl(amount: float) -> dict:
    """Withdraw from master HL balance to its own Arb address ($1 fee).
    Used for periodic replenishment of Arb USDC pool."""
    if not MASTER_WALLET_KEY or not MASTER_WALLET_ADDRESS:
        raise ValueError("Master wallet not configured")
    return withdraw_from_hl(MASTER_WALLET_KEY, amount, MASTER_WALLET_ADDRESS)


# ═══════════════════════════════════════════════════════
# Transfer USDC on Arb (generic)
# ═══════════════════════════════════════════════════════

def transfer_usdc_to_user(private_key: str, to_address: str, amount: float) -> str:
    w3 = get_web3()
    acct = Account.from_key(private_key)
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    amount_raw = int(amount * 1e6)
    max_fee, max_priority = _get_eip1559_fees(w3)

    tx = usdc.functions.transfer(
        Web3.to_checksum_address(to_address), amount_raw
    ).build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 100_000,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": max_priority,
        "chainId": 42161,
        "type": 2,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    logger.info(f"Transferred {amount} USDC to {to_address}, tx: {receipt.transactionHash.hex()}")
    return receipt.transactionHash.hex()


# ═══════════════════════════════════════════════════════
# Execute copy trade with builder code
# ═══════════════════════════════════════════════════════

def execute_copy_trade(
    private_key: str,
    coin: str,
    is_buy: bool,
    size: float,
    price: float,
    reduce_only: bool = False,
    builder_bps: int | None = None,
) -> dict:
    """
    Execute a trade on HyperLiquid with builder fee.

    Args:
        builder_bps: Override builder fee in basis points.
                     None = use default (BUILDER_FEE from env).
                     0 = no fee (free trade via referral).
    """
    from hyperliquid.exchange import Exchange
    import eth_account as eth_acc

    acct = eth_acc.Account.from_key(private_key)
    exchange = Exchange(wallet=acct, base_url="https://api.hyperliquid.xyz")

    # Determine fee: explicit override > env default
    fee_bps = builder_bps if builder_bps is not None else BUILDER_FEE

    # Build order kwargs
    order_kwargs = dict(
        name=coin,
        is_buy=is_buy,
        sz=size,
        limit_px=price,
        order_type={"limit": {"tif": "Ioc"}},
        reduce_only=reduce_only,
    )

    # Only attach builder if fee > 0 and address is set
    if fee_bps > 0 and BUILDER_ADDRESS:
        order_kwargs["builder"] = {"b": BUILDER_ADDRESS, "f": fee_bps}

    result = exchange.order(**order_kwargs)

    logger.info(
        f"Trade: {coin} {'BUY' if is_buy else 'SELL'} {size} @ {price} "
        f"(fee={fee_bps}bps, reduce_only={reduce_only}): {result}"
    )
    return result