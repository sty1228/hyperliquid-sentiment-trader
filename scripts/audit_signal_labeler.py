"""
Synthetic audit of `llm_batch_label` against a fixture of known-failure cases
and anti-regression positives. Calls the REAL OpenAI prompt (not mocked).

Use this to validate prompt / few-shot changes before they ship — when a fix
flips an anti-regression case to is_signal=False, tighten the few-shots and
re-run before committing.

Usage:
  OPENAI_API_KEY=sk-... python -m scripts.audit_signal_labeler
  # or pick up from .env:
  python -m scripts.audit_signal_labeler

The cost is one llm_batch_label call (one round-trip, batched), typically a
few cents at most.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# Avoid the X bearer-token check in module init.
os.environ.setdefault("X_BEARER_TOKEN", "audit")

from backend.ingestor.main import llm_batch_label  # noqa: E402


@dataclass
class Case:
    tweet: str
    expected_is_signal: bool
    expected_direction: str | None     # "long" | "short" | None (when not a signal)
    label: str                         # short tag for the confusion matrix

    def expected(self) -> tuple[bool, str | None]:
        return self.expected_is_signal, self.expected_direction


# ── Fixture ──────────────────────────────────────────────────────
# Cases grouped by failure mode. Each must hold under any new prompt.
CASES: list[Case] = [
    # ───── Failure mode 1: liquidation news (NOT signals) ─────
    Case(
        "💥 BREAKING: $270 million worth of short positions liquidated in the last 1 hour after Trump said tariffs would be reduced",
        False, None, "liq-news shorts (TRUMP)",
    ),
    Case(
        "Massive long liquidation cascade — $850M wiped out as $BTC dumps to 60k",
        False, None, "liq-news longs",
    ),
    Case(
        "$1.2B in liquidations across crypto in the last 24h. Brutal session.",
        False, None, "liq-news both sides",
    ),

    # ───── Failure mode 2: close-not-open (NOT signals) ─────
    Case(
        "Full TP on our $ETH short. Want in? 👇",
        False, None, "close-TP eth short (prod)",
    ),
    Case(
        "Scaled out of my $SOL position — nice ride from $98",
        False, None, "scale-out long",
    ),
    Case(
        "Closed my $BTC long at 72k, locking in profits 🙏",
        False, None, "close long",
    ),

    # ───── Anti-regression: bullish-vibe positives (MUST stay long signals) ─────
    Case(
        "$HYPE to the moon, this is the most bullish setup I've seen in months",
        True, "long", "bullish vibes hype",
    ),
    Case(
        "$SOL is going to ascend to $300, mark my words",
        True, "long", "ascend prediction sol",
    ),
    Case(
        "$ETH dump incoming. Looks weak below 2k.",
        True, "short", "dump incoming eth",
    ),
    Case(
        "Loaded $INJ here, this is one of the most undervalued L1s. Target $50+.",
        True, "long", "loaded inj",
    ),

    # ───── Unambiguous noise (MUST stay False) ─────
    Case(
        "GM CT! Hope everyone's having a great day 🌞",
        False, None, "gm noise",
    ),
    Case(
        "What do you guys think about $ETH? Buy here or wait?",
        False, None, "question noise",
    ),
    Case(
        "🧵 Thread: my top 5 alts for 2025. Like and RT!",
        False, None, "thread bait",
    ),
]


def _verdict(label_dict: dict) -> tuple[bool, str | None]:
    is_sig = bool(label_dict.get("is_signal"))
    direction = (label_dict.get("direction") or "").lower() if is_sig else None
    if direction not in ("long", "short"):
        direction = None if not is_sig else direction
    return is_sig, direction


def main() -> int:
    items = [{"id": str(i), "tweet": c.tweet} for i, c in enumerate(CASES)]
    labels, tin, tout = llm_batch_label(items)

    pass_n, fail_n = 0, 0
    rows = []
    for i, c in enumerate(CASES):
        got = labels.get(str(i), {})
        got_sig, got_dir = _verdict(got)
        exp_sig, exp_dir = c.expected()

        # Direction is only checked when the case is meant to be a signal.
        sig_ok = got_sig == exp_sig
        dir_ok = (not exp_sig) or (got_dir == exp_dir)
        ok = sig_ok and dir_ok

        rows.append((c.label, exp_sig, exp_dir, got_sig, got_dir, got.get("confidence", 0), ok))
        if ok:
            pass_n += 1
        else:
            fail_n += 1

    # ── Print confusion matrix ──
    print()
    print(f"{'case':32} | {'expected':18} | {'got':22} | conf | ok")
    print("-" * 96)
    for label, e_sig, e_dir, g_sig, g_dir, conf, ok in rows:
        e_str = f"{'sig=' + (e_dir or '?') if e_sig else 'NOISE'}"
        g_str = f"{'sig=' + (g_dir or '?') if g_sig else 'NOISE'}"
        mark = "✓" if ok else "✗"
        print(f"{label:32} | {e_str:18} | {g_str:22} | {conf:>4} | {mark}")
    print("-" * 96)
    print(f"PASS {pass_n}/{len(CASES)}    FAIL {fail_n}    "
          f"tokens in={tin} out={tout}")

    # Group-level summaries — useful for spotting partial regressions.
    print()
    groups = {
        "liquidation-news (must be NOISE)":   range(0, 3),
        "close-not-open (must be NOISE)":     range(3, 6),
        "bullish-vibe (must be SIGNAL)":      range(6, 10),
        "unambiguous-noise (must be NOISE)":  range(10, 13),
    }
    for name, idxs in groups.items():
        sub_pass = sum(1 for i in idxs if rows[i][6])
        sub_total = len(list(idxs))
        print(f"  {name:42} {sub_pass}/{sub_total}")

    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
