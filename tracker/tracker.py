#!/usr/bin/env python3
"""
Robinhood Chain -> Arbitrum AEP revenue tracker.

Tracks the two leading indicators of Robinhood dialing chain REV down
relative to activity:

  1. The Nitro gas-pricing parameters Robinhood controls as chain owner
     (speed limit, minimum base fee, pricing inertia, backlog tolerance,
     L1 reward rate, fee accounts, ArbOS version). Any change is flagged.

  2. Realized fee-per-gas vs gas consumed, reconstructed block-by-block
     from eth_feeHistory (sum of baseFeePerGas * gasUsed), and the implied
     10% AEP remittance on that base. Rising gas with flat/falling fee-per-gas
     = REV being decoupled from activity.

Stdlib only. Designed to run every 12h under GitHub Actions (or cron) and
append to data/history.csv; renders docs/index.html + docs/latest.md.
"""

import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
HISTORY = os.path.join(DATA_DIR, "history.csv")
LATEST = os.path.join(DATA_DIR, "latest.json")

# Nitro precompiles (identical on every Nitro chain)
ARB_GAS_INFO = "0x000000000000000000000000000000000000006C"
ARB_OWNER_PUBLIC = "0x000000000000000000000000000000000000006b"
ARB_SYS = "0x0000000000000000000000000000000000000064"

# 4-byte selectors (keccak256 of the signature)
SEL = {
    "getMinimumGasPrice()": "0xf918379a",
    "getL1BaseFeeEstimate()": "0xf5d6ded7",
    "getGasBacklog()": "0x1d5b5c20",
    "getPricesInWei()": "0x41b247a8",
    "getGasAccountingParams()": "0x612af178",
    "getL2BaseFeeEstimate()": "0xb246b565",
    "getPricingInertia()": "0x3dfb45b9",
    "getGasBacklogTolerance()": "0x25754f91",
    "getPerBatchGasCharge()": "0x6ecca45a",
    "getAmortizedCostCapBips()": "0x7a7d6beb",
    "getL1PricingSurplus()": "0x520acdd7",
    "getL1RewardRate()": "0x8a5b1d28",
    "getL1RewardRecipient()": "0x9e6d7e31",
    "getNetworkFeeAccount()": "0x2d9125e9",
    "getInfraFeeAccount()": "0xee95a824",
    "arbOSVersion()": "0x051038f2",
}

# Parameters whose change is a policy signal (the "dial")
POLICY_PARAMS = [
    "speed_limit_gas_per_sec",
    "gas_pool_max",
    "max_tx_gas_limit",
    "min_base_fee_gwei",
    "pricing_inertia",
    "backlog_tolerance",
    "l1_reward_rate",
    "l1_reward_recipient",
    "network_fee_account",
    "infra_fee_account",
    "arbos_version",
]

CSV_FIELDS = [
    "run_utc", "window_start_utc", "window_end_utc", "window_hours",
    "start_block", "end_block", "blocks",
    "gas_used", "gas_per_sec",
    "rev_eth", "rev_usd", "eth_usd",
    "fee_per_gas_gwei", "avg_base_fee_gwei",
    "implied_aep_eth", "implied_aep_usd",
    "speed_limit_gas_per_sec", "gas_pool_max", "max_tx_gas_limit",
    "min_base_fee_gwei", "pricing_inertia", "backlog_tolerance",
    "l1_reward_rate", "l1_reward_recipient",
    "network_fee_account", "infra_fee_account", "arbos_version",
    "l2_base_fee_now_gwei", "l1_base_fee_est_gwei", "gas_backlog",
    "l1_pricing_surplus_eth",
    "llama_rev_24h_usd", "llama_arb_one_rev_24h_usd",
    "aep_recipient_inflow_eth",
    "regime", "param_changes", "fee_method",
]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cfg():
    with open(CFG_PATH) as f:
        return json.load(f)


def http_json(url, payload=None, timeout=30, headers=None):
    hdrs = {"Content-Type": "application/json", "User-Agent": "rhc-rev-tracker/1.0"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class RPC:
    def __init__(self, urls, sleep=0.05):
        self.urls = urls
        self.sleep = sleep
        self.i = 0
        self.calls = 0

    def call(self, method, params, retries=4):
        last = None
        for attempt in range(retries):
            url = self.urls[self.i % len(self.urls)]
            try:
                out = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                self.calls += 1
                if "error" in out:
                    raise RuntimeError(f"{method}: {out['error']}")
                time.sleep(self.sleep)
                return out["result"]
            except Exception as e:  # noqa
                last = e
                self.i += 1
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"RPC failed for {method}: {last}")

    def batch(self, calls, retries=3):
        """JSON-RPC batch: calls = [(method, params), ...]. Returns list of results in order."""
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        last = None
        for attempt in range(retries):
            url = self.urls[self.i % len(self.urls)]
            try:
                out = http_json(url, payload, timeout=60)
                self.calls += 1
                if not isinstance(out, list):
                    raise RuntimeError(f"batch: non-list response {str(out)[:120]}")
                by_id = {o.get("id"): o for o in out}
                res = []
                for i in range(len(calls)):
                    o = by_id.get(i)
                    if o is None or "error" in o:
                        raise RuntimeError(f"batch item {i}: {o.get('error') if o else 'missing'}")
                    res.append(o["result"])
                time.sleep(self.sleep)
                return res
            except Exception as e:  # noqa
                last = e
                self.i += 1
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"RPC batch failed: {last}")

    def eth_call(self, to, selector, block="latest"):
        return self.call("eth_call", [{"to": to, "data": selector}, block])


def u256s(hexdata):
    """Decode a return blob into a list of uint256 words."""
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def addr(hexdata):
    return "0x" + hexdata[-40:].lower()


def gwei(wei):
    return wei / 1e9


def eth(wei):
    return wei / 1e18


# ----------------------------------------------------------------------
# chain reads
# ----------------------------------------------------------------------

def read_params(rpc, block_tag):
    p = {}
    acc = u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getGasAccountingParams()"], block_tag))
    p["speed_limit_gas_per_sec"] = acc[0]
    p["gas_pool_max"] = acc[1]
    p["max_tx_gas_limit"] = acc[2]
    p["min_base_fee_gwei"] = gwei(u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getMinimumGasPrice()"], block_tag))[0])
    p["pricing_inertia"] = u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getPricingInertia()"], block_tag))[0]
    p["backlog_tolerance"] = u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getGasBacklogTolerance()"], block_tag))[0]
    p["l1_reward_rate"] = u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getL1RewardRate()"], block_tag))[0]
    p["l1_reward_recipient"] = addr(rpc.eth_call(ARB_GAS_INFO, SEL["getL1RewardRecipient()"], block_tag))
    p["network_fee_account"] = addr(rpc.eth_call(ARB_OWNER_PUBLIC, SEL["getNetworkFeeAccount()"], block_tag))
    p["infra_fee_account"] = addr(rpc.eth_call(ARB_OWNER_PUBLIC, SEL["getInfraFeeAccount()"], block_tag))
    p["arbos_version"] = u256s(rpc.eth_call(ARB_SYS, SEL["arbOSVersion()"], block_tag))[0]
    # state (not policy) reads
    p["l2_base_fee_now_gwei"] = gwei(u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getL2BaseFeeEstimate()"], block_tag))[0])
    p["l1_base_fee_est_gwei"] = gwei(u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getL1BaseFeeEstimate()"], block_tag))[0])
    p["gas_backlog"] = u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getGasBacklog()"], block_tag))[0]
    surplus = u256s(rpc.eth_call(ARB_GAS_INFO, SEL["getL1PricingSurplus()"], block_tag))[0]
    if surplus >= 2 ** 255:  # int256 negative
        surplus -= 2 ** 256
    p["l1_pricing_surplus_eth"] = eth(surplus)
    return p


def block_ts(rpc, n):
    b = rpc.call("eth_getBlockByNumber", [hex(n), False])
    return int(b["timestamp"], 16), int(b["gasLimit"], 16)


def find_block_at(rpc, target_ts, lo, hi):
    """Binary search for first block with timestamp >= target_ts."""
    while lo < hi:
        mid = (lo + hi) // 2
        ts, _ = block_ts(rpc, mid)
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def fee_window(rpc, start_block, end_block, chunk, gas_limit, stride=20, batch_size=100):
    """
    Sum baseFeePerGas * gasUsed over (start_block, end_block].
    Tries eth_feeHistory (exact, per block). Public Nitro RPCs only serve feeHistory
    for the most recent few thousand blocks ("metadata is not found" beyond that), so on
    that error the remaining range is reconstructed from block headers sampled every
    `stride` blocks via batched eth_getBlockByNumber and scaled to the full range.
    Returns (gas_used_total, rev_wei_total, blocks_counted, sum_basefee_wei, method).
    """
    gas_total = 0.0
    rev_wei = 0.0
    fee_sum = 0
    n_blocks = 0
    newest = end_block
    method = "feeHistory"
    while newest > start_block:
        count = min(chunk, newest - start_block)
        try:
            fh = rpc.call("eth_feeHistory", [hex(count), hex(newest), []], retries=2)
        except RuntimeError as e:
            log(f"feeHistory unavailable at block {newest} ({str(e)[:80]}); falling back to sampled headers for ({start_block}, {newest}]")
            g2, r2, n2, f2 = _header_window(rpc, start_block, newest, gas_limit, stride, batch_size)
            gas_total += g2; rev_wei += r2; n_blocks += n2; fee_sum += f2
            method = "feeHistory+headers" if n_blocks > n2 else "headers"
            break
        base = [int(x, 16) for x in fh["baseFeePerGas"]]
        ratios = fh["gasUsedRatio"]
        oldest = int(fh["oldestBlock"], 16)
        got = len(ratios)
        if got == 0:
            break
        for i in range(got):
            gu = ratios[i] * gas_limit
            gas_total += gu
            rev_wei += base[i] * gu
            fee_sum += base[i]
            n_blocks += 1
        newest = oldest - 1
    return int(gas_total), rev_wei, n_blocks, fee_sum, method


def _header_window(rpc, start_block, end_block, gas_limit, stride, batch_size):
    """Sample every `stride` blocks in (start_block, end_block], scale sums by stride."""
    span = end_block - start_block
    if span <= 0:
        return 0, 0.0, 0, 0
    stride = max(1, min(stride, span))
    blocks = list(range(end_block, start_block, -stride))
    gas = 0.0
    rev = 0.0
    fee = 0
    n = 0
    for i in range(0, len(blocks), batch_size):
        part = blocks[i:i + batch_size]
        try:
            res = rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in part])
        except RuntimeError as e:
            log(f"batch failed ({str(e)[:80]}); retrying this chunk with single calls")
            res = [rpc.call("eth_getBlockByNumber", [hex(b), False]) for b in part]
        for b in res:
            if not b:
                continue
            bf = int(b.get("baseFeePerGas", "0x0"), 16)
            gu = int(b.get("gasUsed", "0x0"), 16)
            gas += gu
            rev += bf * gu
            fee += bf
            n += 1
    if n == 0:
        raise RuntimeError("header sampling returned no blocks")
    scale = span / n
    log(f"sampled {n} headers over {span} blocks (stride {stride}, scale {scale:.1f}x)")
    return gas * scale, rev * scale, span, int(fee * scale)


# ----------------------------------------------------------------------
# external cross-checks (non-fatal)
# ----------------------------------------------------------------------

def eth_price():
    try:
        out = http_json("https://coins.llama.fi/prices/current/coingecko:ethereum")
        return float(out["coins"]["coingecko:ethereum"]["price"])
    except Exception as e:  # noqa
        log(f"eth price fetch failed: {e}")
        return None


def llama_chain_rev_24h(slugs):
    for s in slugs:
        try:
            out = http_json(f"https://api.llama.fi/summary/fees/{s}?dataType=dailyRevenue")
            v = out.get("total24h")
            if v is not None:
                return float(v), s
        except Exception:
            continue
    return None, None


def blockscout_inflow(api, address, start_block, end_block):
    """Sum of ETH internal-tx value into `address` within the block range."""
    if not address:
        return None
    try:
        url = (f"{api}?module=account&action=txlistinternal&address={address}"
               f"&startblock={start_block}&endblock={end_block}&sort=asc")
        out = http_json(url)
        total = 0
        for tx in out.get("result", []) or []:
            if str(tx.get("to", "")).lower() == address.lower():
                total += int(tx.get("value", "0"))
        return eth(total)
    except Exception as e:  # noqa
        log(f"blockscout inflow failed: {e}")
        return None


# ----------------------------------------------------------------------
# history / signals
# ----------------------------------------------------------------------

def load_history():
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY) as f:
        return list(csv.DictReader(f))


def append_history(row):
    exists = os.path.exists(HISTORY)
    with open(HISTORY, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def load_latest():
    if os.path.exists(LATEST):
        with open(LATEST) as f:
            return json.load(f)
    return None


def detect_param_changes(prev, cur):
    changes = []
    if not prev:
        return changes
    for k in POLICY_PARAMS:
        a, b = prev.get(k), cur.get(k)
        if a is None or b is None:
            continue
        if isinstance(a, float) or isinstance(b, float):
            if abs(float(a) - float(b)) > 1e-12:
                changes.append(f"{k}: {a} -> {b}")
        elif str(a) != str(b):
            changes.append(f"{k}: {a} -> {b}")
    return changes


def classify_regime(hist_rows, cur, n):
    """
    Compare current run to the mean of the prior n runs.
    gas up & fee/gas down  -> DECOUPLING (the tell)
    gas up & fee/gas up    -> CONGESTION_PRICING
    gas down & fee/gas down-> DECAY
    else                   -> FLAT / MIXED
    """
    rows = [r for r in hist_rows if r.get("gas_per_sec") and r.get("fee_per_gas_gwei")]
    if len(rows) < 2:
        return "WARMUP"
    prior = rows[-n:]
    g0 = sum(float(r["gas_per_sec"]) for r in prior) / len(prior)
    f0 = sum(float(r["fee_per_gas_gwei"]) for r in prior) / len(prior)
    g1, f1 = cur["gas_per_sec"], cur["fee_per_gas_gwei"]
    if g0 <= 0 or f0 <= 0:
        return "WARMUP"
    dg = (g1 - g0) / g0
    df = (f1 - f0) / f0
    thr = 0.10
    if dg > thr and df < -thr:
        return "DECOUPLING"
    if dg > thr and df > thr:
        return "CONGESTION_PRICING"
    if dg < -thr and df < -thr:
        return "DECAY"
    if abs(dg) <= thr and abs(df) <= thr:
        return "FLAT"
    return "MIXED"


def rolling(hist_rows, hours):
    """Sum rev/gas over the trailing `hours` of runs; returns (rev_usd, gas, fee_per_gas)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rev = gas = 0.0
    for r in hist_rows:
        try:
            t = datetime.fromisoformat(r["run_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t >= cutoff:
            rev += float(r.get("rev_usd") or 0)
            gas += float(r.get("gas_used") or 0)
    fpg = (rev / gas) if gas else 0.0
    return rev, gas, fpg


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------

def fmt(v, kind=""):
    if v is None or v == "":
        return "—"
    try:
        v = float(v)
    except Exception:
        return str(v)
    if kind == "usd":
        return f"${v:,.0f}"
    if kind == "eth":
        return f"{v:,.4f} ETH"
    if kind == "gwei":
        return f"{v:,.4f} gwei"
    if kind == "int":
        return f"{v:,.0f}"
    return f"{v:,.4g}"


def render(hist_rows, cur, changes, roll7, roll7_prev):
    """Delegate to render.py (rich HTML) and write the vault note."""
    os.makedirs(DOCS_DIR, exist_ok=True)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import render as R
    rows, latest = R.load(HISTORY, LATEST)
    R.render(rows, latest, None, os.path.join(DOCS_DIR, "index.html"))
    md = f"""---
run: {cur['run_utc']}
regime: {cur['regime']}
rev_usd: {fmt(cur['rev_usd'],'usd')}
fee_per_gas_gwei: {cur['fee_per_gas_gwei']:.5f}
gas_per_sec: {cur['gas_per_sec']:.0f}
implied_aep_usd: {fmt(cur['implied_aep_usd'],'usd')}
param_changes: {len(changes)}
---
# Robinhood Chain → AEP tracker — {cur['run_utc'][:16]}

**Regime:** {cur['regime']}  ·  **Window:** {cur['window_hours']:.1f}h, blocks {cur['start_block']}–{cur['end_block']}

| metric | value |
|---|---|
| Chain REV | {fmt(cur['rev_usd'],'usd')} ({fmt(cur['rev_eth'],'eth')}) |
| Implied AEP (10% gross) | {fmt(cur['implied_aep_usd'],'usd')} |
| Fee per gas | {fmt(cur['fee_per_gas_gwei'],'gwei')} |
| Gas / sec (vs speed limit) | {fmt(cur['gas_per_sec'],'int')} / {fmt(cur['speed_limit_gas_per_sec'],'int')} |
| Trailing 7d REV | {fmt(roll7[0],'usd')} |

**Parameter changes:** {'; '.join(changes) if changes else 'none'}
"""
    with open(os.path.join(DOCS_DIR, "latest.md"), "w") as f:
        f.write(md)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main():
    cfg = load_cfg()
    os.makedirs(DATA_DIR, exist_ok=True)
    rpc = RPC(cfg["rpc_urls"], cfg.get("rpc_sleep_seconds", 0.05))
    hist = load_history()
    prev = load_latest()

    now = datetime.now(timezone.utc)
    end_block = int(rpc.call("eth_blockNumber", []), 16)
    end_ts, gas_limit = block_ts(rpc, end_block)
    log(f"head block {end_block} @ {datetime.fromtimestamp(end_ts, timezone.utc)} gasLimit={gas_limit}")

    # window: contiguous with last run if available and not too stale
    start_block = None
    if prev and prev.get("end_block"):
        age_h = (end_ts - int(prev["end_ts"])) / 3600
        if 0 < age_h <= cfg.get("max_window_hours", 48):
            start_block = int(prev["end_block"])
    if start_block is None:
        target = end_ts - cfg["window_hours"] * 3600
        # bound the search: assume >= 0.1 blocks/sec to pick a low guess, then binary search
        lo = max(0, end_block - int(cfg["window_hours"] * 3600 * 20))
        start_block = find_block_at(rpc, target, lo, end_block)
    start_ts, _ = block_ts(rpc, start_block)
    window_hours = (end_ts - start_ts) / 3600 or 1e-9
    log(f"window blocks ({start_block}, {end_block}] = {end_block - start_block} blocks, {window_hours:.2f}h")

    gas_used, rev_wei, n_blocks, fee_sum, fee_method = fee_window(
        rpc, start_block, end_block, cfg["fee_history_chunk"], gas_limit,
        cfg.get("header_sample_stride", 20), cfg.get("rpc_batch_size", 100))
    log(f"gas_used={gas_used:,} rev={eth(rev_wei):.4f} ETH over {n_blocks} blocks ({rpc.calls} rpc calls)")

    params = read_params(rpc, hex(end_block))
    px = eth_price()
    rev_eth = eth(rev_wei)
    rev_usd = rev_eth * px if px else None
    fee_per_gas_gwei = gwei(rev_wei / gas_used) if gas_used else 0.0
    avg_base_fee_gwei = gwei(fee_sum / n_blocks) if n_blocks else 0.0
    share = cfg.get("aep_share", 0.10)

    llama_rev, slug = llama_chain_rev_24h(cfg.get("defillama_chain_slugs", []))
    llama_arb, _ = llama_chain_rev_24h([cfg.get("defillama_arbitrum_slug", "arbitrum")])
    inflow = blockscout_inflow(cfg.get("blockscout_api", ""), cfg.get("aep_recipient_address", ""), start_block, end_block)

    cur = {
        "run_utc": now.isoformat(timespec="seconds"),
        "window_start_utc": datetime.fromtimestamp(start_ts, timezone.utc).isoformat(timespec="seconds"),
        "window_end_utc": datetime.fromtimestamp(end_ts, timezone.utc).isoformat(timespec="seconds"),
        "window_hours": window_hours,
        "start_block": start_block, "end_block": end_block, "end_ts": end_ts, "blocks": n_blocks,
        "gas_used": gas_used,
        "gas_per_sec": gas_used / (window_hours * 3600),
        "rev_eth": rev_eth, "rev_usd": rev_usd, "eth_usd": px,
        "fee_per_gas_gwei": fee_per_gas_gwei, "avg_base_fee_gwei": avg_base_fee_gwei,
        "implied_aep_eth": rev_eth * share,
        "implied_aep_usd": (rev_usd * share) if rev_usd is not None else None,
        "llama_rev_24h_usd": llama_rev, "llama_arb_one_rev_24h_usd": llama_arb,
        "aep_recipient_inflow_eth": inflow,
        "rpc_calls": rpc.calls,
        "fee_method": fee_method,
    }
    cur.update(params)

    changes = detect_param_changes(prev, cur)
    cur["param_changes"] = " | ".join(changes)
    cur["regime"] = classify_regime(hist, cur, cfg.get("trend_runs", 6))

    append_history(cur)
    hist = load_history()
    roll7 = rolling(hist, 24 * 7)
    # prior 7d: rows in (14d, 7d]
    cutoff_hi = datetime.now(timezone.utc) - timedelta(days=7)
    rev = gas = 0.0
    for r in hist:
        try:
            t = datetime.fromisoformat(r["run_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        if cutoff_hi - timedelta(days=7) <= t < cutoff_hi:
            rev += float(r.get("rev_usd") or 0)
            gas += float(r.get("gas_used") or 0)
    roll7_prev = (rev, gas, (rev / gas) if gas else 0.0)

    with open(LATEST, "w") as f:
        json.dump(cur, f, indent=2, default=str)
    render(hist, cur, changes, roll7, roll7_prev)

    print("\n=== SUMMARY ===")
    print(f"regime           : {cur['regime']}")
    print(f"REV (window)     : {fmt(cur['rev_usd'],'usd')}  ({rev_eth:.4f} ETH)  fee/gas {fee_per_gas_gwei:.5f} gwei")
    print(f"gas/sec          : {cur['gas_per_sec']:,.0f}  vs speed limit {params['speed_limit_gas_per_sec']:,}")
    print(f"implied AEP 10%  : {fmt(cur['implied_aep_usd'],'usd')}")
    print(f"param changes    : {changes or 'none'}")
    if llama_rev is not None:
        print(f"DefiLlama 24h    : ${llama_rev:,.0f} (slug {slug})")
    if changes or cur["regime"] == "DECOUPLING":
        print("::warning::SIGNAL: " + (cur["regime"] if cur["regime"] == "DECOUPLING" else "") + " " + "; ".join(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
