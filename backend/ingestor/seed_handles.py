"""
v1 alpha-leaning seed list for the Apify ingestor (INGESTOR_SOURCE=apify).

TEMPORARY — replaced once the Airtable Master Frequency List sync from Ethan
lands and we can derive the list from
    SELECT username FROM traders WHERE frequency_score >= X
The list below is a curated subset of `DEFAULT_USERS` (in main.py) skewed
toward consistently directional posters; broad-market influencers
(saylor, whale_alert, AltcoinDaily, etc.) are intentionally excluded — they
produced most of the noise in the X-API era.

Lowercased once at import; downstream code should never need .lower().
"""
from __future__ import annotations

_RAW_HANDLES: list[str] = [
    # Top-tier alpha / trader voices
    "balajis", "cbb0fe", "icebergy", "Bluntz_Capital", "krugermacro",
    "HsakaTrades", "Pentosh1", "GCRClassic", "cobie", "RookieXBT",
    "DonAlt", "CryptoCred", "Tradermayne", "ledgerstatus", "CL207",
    "Tree_of_Alpha", "SmartContracter", "TheFlowHorse", "DaanCrypto",
    "satsdart", "jukan05", "RektProof", "ByzGeneral", "egirl_capital",
    "AltcoinPsycho", "Ninjascalp", "abetrade", "tradingriot", "BigCheds",
    "Citrini7", "Nebraskangooner", "jimtalbot", "gainzy222", "inversebrah",
    "tradingstable", "MuroCrypto", "CryptoJelleNL", "NukeCapital",
    "TraderMercury", "Trader_XO", "IncomeSharks", "CryptoTony__",
    "CryptoGodJohn", "EmperorBTC", "PeterLBrandt", "TheCryptoDog",
    "bitcoinjack", "insomniacxbt", "Tom__Capital", "flopxbt", "docXBT",
    "Danny_Crypton", "CryptoWizardd", "trader_koala", "LomahCrypto",
    "CredibleCrypto", "CryptoCaesarTA", "Crypto_Chase", "ThinkingUSD",
    "PriorXBT", "yourQuantGuy", "0xThoor", "coinmamba", "CryptoPoseidonn",
    "papagiorgioXBT", "PillageCapital", "macklorden", "kaceohhh",
    "PhoenixBtcFire", "QuantMeta", "TechCharts", "alphawhaletrade",
    "LightCrypto", "MomentumKevin", "TheWhiteWhaleHL", "JamesWynnReal",
    "KeyboardMonkey3", "basedkarbon", "Rijk__", "HYPEconomist", "MizerXBT",
    "DefiIgnas", "MustStopMurad", "blknoiz06", "GiganticRebirth",
    "TedPillows", "izebel_eth", "defi_mochi", "CryptoHayes", "arthur0x",
    "Rewkang", "QwQiao", "ThinkingBitmex", "theunipcs", "scottmelker",
    "toly", "aixbt_agent", "MacroCRG", "Cryptopathic", "AltcoinSherpa",
    "ColdBloodShill", "DegenSpartan", "pierre_crypt0",
]

APIFY_SEED_HANDLES: list[str] = [h.lower() for h in _RAW_HANDLES]
