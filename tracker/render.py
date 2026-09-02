#!/usr/bin/env python3
"""
Render the Robinhood Chain -> Arbitrum AEP brief as a single self-contained HTML file.

Usage:
  python render.py --history data/history.csv --latest data/latest.json --out docs/index.html
                   [--commentary commentary.md]   # analyst commentary to inject (markdown-lite)

Without --commentary, a rule-based commentary is generated from the data.
No external assets; safe to open from a local synced folder offline.
"""

import argparse
import csv
import json
import html as H
from datetime import datetime, timezone, timedelta, date

SUBSIDY_END = date(2026, 9, 29)
CHANGE_DATE = date(2030, 12, 31)


# ----------------------------------------------------------------------
# data
# ----------------------------------------------------------------------

def f(v):
    try:
        return float(v)
    except Exception:
        return None


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load(history_path, latest_path):
    rows = []
    with open(history_path) as fh:
        for r in csv.DictReader(fh):
            r["_t"] = parse_ts(r.get("window_end_utc") or r.get("run_utc", ""))
            rows.append(r)
    rows = [r for r in rows if r["_t"]]
    rows.sort(key=lambda r: r["_t"])
    with open(latest_path) as fh:
        latest = json.load(fh)
    return rows, latest


def daily(rows):
    """Aggregate runs to UTC days: rev_usd, rev_eth, gas, fee/gas, gas/sec, aep."""
    days = {}
    for r in rows:
        d = r["_t"].date().isoformat()
        a = days.setdefault(d, {"rev_usd": 0.0, "rev_eth": 0.0, "gas": 0.0, "hours": 0.0, "aep_usd": 0.0, "n": 0})
        a["rev_usd"] += f(r.get("rev_usd")) or 0
        a["rev_eth"] += f(r.get("rev_eth")) or 0
        a["gas"] += f(r.get("gas_used")) or 0
        a["hours"] += f(r.get("window_hours")) or 0
        a["aep_usd"] += f(r.get("implied_aep_usd")) or 0
        a["limit"] = f(r.get("speed_limit_gas_per_sec")) or a.get("limit")
        a["n"] += 1
    out = []
    for d in sorted(days):
        a = days[d]
        a["date"] = d
        a["fee_per_gas_gwei"] = (a["rev_eth"] * 1e9 / a["gas"]) if a["gas"] else None
        a["gas_per_sec"] = (a["gas"] / (a["hours"] * 3600)) if a["hours"] else None
        a["rev_usd_per_hour"] = (a["rev_usd"] / a["hours"]) if a["hours"] else None
        out.append(a)
    return out


def window_sum(rows, lo_h, hi_h):
    """Sum over runs with age in (lo_h, hi_h] hours."""
    now = datetime.now(timezone.utc)
    rev = gas = eth = hrs = 0.0
    for r in rows:
        age = (now - r["_t"]).total_seconds() / 3600
        if lo_h < age <= hi_h:
            rev += f(r.get("rev_usd")) or 0
            eth += f(r.get("rev_eth")) or 0
            gas += f(r.get("gas_used")) or 0
            hrs += f(r.get("window_hours")) or 0
    return {"rev_usd": rev, "rev_eth": eth, "gas": gas, "hours": hrs,
            "fee_per_gas_gwei": (eth * 1e9 / gas) if gas else None,
            "gas_per_sec": (gas / (hrs * 3600)) if hrs else None}


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a - b) / b * 100


# ----------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------

def usd(v, d=0):
    return "—" if v is None else f"${v:,.{d}f}"


def num(v, d=0):
    return "—" if v is None else f"{v:,.{d}f}"


def gw(v):
    return "—" if v is None else f"{v:,.4f} gwei"


def sgn(v, d=1):
    return "—" if v is None else f"{v:+.{d}f}%"


def esc(s):
    return H.escape(str(s))


# ----------------------------------------------------------------------
# charts (inline SVG, no deps)
# ----------------------------------------------------------------------

W, HGT, PL, PR, PT, PB = 900, 230, 62, 62, 18, 34


def _scale(vals, lo=None):
    vs = [v for v in vals if v is not None]
    if not vs:
        return 0, 1
    mn = 0 if lo == 0 else min(vs)
    mx = max(vs)
    if mx == mn:
        mx = mn + 1
    return mn, mx


def _ticks(mn, mx, n=4):
    return [mn + (mx - mn) * i / n for i in range(n + 1)]


def _fmt_tick(v, kind):
    if kind == "usd":
        return f"${v/1e6:.2f}M" if abs(v) >= 1e6 else (f"${v/1e3:.0f}k" if abs(v) >= 1e3 else f"${v:.0f}")
    if kind == "gwei":
        return f"{v:.3f}"
    if kind == "gps":
        return f"{v/1e6:.1f}M"
    if kind == "pct":
        return f"{v:.0f}%"
    return f"{v:,.3g}"


def chart(title, labels, series, kinds, note=""):
    """
    series: list of dicts {name, values, color, kind:'bar'|'line', axis:'l'|'r'}
    kinds: {'l': 'usd'|'gwei'|..., 'r': ...}
    """
    n = len(labels)
    if n < 2:
        return f"<div class='chart'><div class='ct'>{esc(title)}</div><div class='muted'>not enough data yet (need ≥2 runs)</div></div>"
    iw = W - PL - PR
    ih = HGT - PT - PB
    ax = {}
    for side in ("l", "r"):
        vals = [v for s in series if s["axis"] == side for v in s["values"]]
        ax[side] = _scale(vals, lo=0)
    parts = [f"<svg viewBox='0 0 {W} {HGT}' width='100%' preserveAspectRatio='none' class='svg'>"]
    # grid + left ticks
    mn, mx = ax["l"]
    for t in _ticks(mn, mx):
        y = PT + ih * (1 - (t - mn) / (mx - mn))
        parts.append(f"<line x1='{PL}' x2='{W-PR}' y1='{y:.1f}' y2='{y:.1f}' class='grid'/>")
        parts.append(f"<text x='{PL-6}' y='{y+4:.1f}' class='tick' text-anchor='end'>{_fmt_tick(t, kinds.get('l'))}</text>")
    if any(s["axis"] == "r" for s in series):
        mn2, mx2 = ax["r"]
        for t in _ticks(mn2, mx2):
            y = PT + ih * (1 - (t - mn2) / (mx2 - mn2))
            parts.append(f"<text x='{W-PR+6}' y='{y+4:.1f}' class='tick'>{_fmt_tick(t, kinds.get('r'))}</text>")
    # x labels (thin out)
    step = max(1, n // 8)
    for i, lab in enumerate(labels):
        if i % step == 0 or i == n - 1:
            x = PL + iw * i / (n - 1)
            parts.append(f"<text x='{x:.1f}' y='{HGT-10}' class='tick' text-anchor='middle'>{esc(lab)}</text>")
    # series
    for s in series:
        mn, mx = ax[s["axis"]]
        pts = []
        if s["kind"] == "bar":
            bw = max(2, iw / n * 0.7)
            for i, v in enumerate(s["values"]):
                if v is None:
                    continue
                x = PL + iw * i / (n - 1) - bw / 2
                y = PT + ih * (1 - (v - mn) / (mx - mn))
                parts.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{PT+ih-y:.1f}' fill='{s['color']}' opacity='0.85'/>")
        else:
            for i, v in enumerate(s["values"]):
                if v is None:
                    continue
                x = PL + iw * i / (n - 1)
                y = PT + ih * (1 - (v - mn) / (mx - mn))
                pts.append(f"{x:.1f},{y:.1f}")
            if pts:
                parts.append(f"<polyline fill='none' stroke='{s['color']}' stroke-width='2.2' points='{' '.join(pts)}'/>")
    parts.append("</svg>")
    legend = " &nbsp; ".join(f"<span class='lg'><i style='background:{s['color']}'></i>{esc(s['name'])}</span>" for s in series)
    return (f"<div class='chart'><div class='ct'>{esc(title)}</div><div class='legend'>{legend}</div>"
            f"{''.join(parts)}<div class='muted small'>{esc(note)}</div></div>")


# ----------------------------------------------------------------------
# commentary (rule-based fallback)
# ----------------------------------------------------------------------

def auto_commentary(rows, latest, d, w24, w24p, w7, w7p):
    today = datetime.now(timezone.utc).date()
    paras = []
    regime = latest.get("regime", "WARMUP")
    changes = [c for c in (latest.get("param_changes") or "").split(" | ") if c]

    # 1. the dial
    if changes:
        paras.append(("Parameter change detected",
                      "Robinhood moved the chain-owner dial since the last run: " + "; ".join(changes) +
                      ". This is the direct signal. Read the next 2–4 runs for whether fee-per-gas compresses at constant or rising gas — "
                      "that would confirm REV is being managed down rather than lost."))
    else:
        recent = [(r["_t"].strftime("%m-%d %H:%M"), r.get("param_changes")) for r in rows
                  if r.get("param_changes") and (datetime.now(timezone.utc) - r["_t"]).days < 7]
        if recent:
            paras.append(("Dial moved within the last 7 days",
                          "No change this run, but the parameter log shows: " +
                          "; ".join(f"{t}: {c}" for t, c in recent) +
                          ". Fee-per-gas behaviour since that change is the thing to read on the charts below."))
        paras.append(("Dial unchanged this run",
                      f"No change to speed limit ({num(f(latest.get('speed_limit_gas_per_sec')))} gas/s), minimum base fee "
                      f"({gw(f(latest.get('min_base_fee_gwei')))}), inertia, backlog tolerance, fee accounts or ArbOS version "
                      f"(v{latest.get('arbos_version')}). Whatever REV did this window, it did on unchanged pricing policy."))

    # 2. regime + REV vs activity
    gps = f(latest.get("gas_per_sec"))
    lim = f(latest.get("speed_limit_gas_per_sec"))
    util = (gps / lim * 100) if gps and lim else None
    dg = pct(w24.get("gas_per_sec"), w24p.get("gas_per_sec"))
    dfpg = pct(w24.get("fee_per_gas_gwei"), w24p.get("fee_per_gas_gwei"))
    drev = pct(w24["rev_usd"], w24p["rev_usd"])
    body = (f"Trailing 24h chain REV {usd(w24['rev_usd'])} ({sgn(drev)} vs the prior 24h) on gas throughput "
            f"{num(w24.get('gas_per_sec'))} gas/s ({sgn(dg)}) at a realized {gw(w24.get('fee_per_gas_gwei'))} per gas ({sgn(dfpg)}). ")
    if util is not None:
        body += (f"Current utilisation is {util:.0f}% of the speed limit — "
                 + ("above the limit, so congestion pricing is active and REV is scaling with activity. "
                    if util >= 100 else
                    "below the limit, so the chain is running near the fee floor and REV per unit activity is at its minimum for the current parameters. "))
    label = {
        "DECOUPLING": "Regime: DECOUPLING. Gas is up while fee-per-gas is down. This is the pattern of REV being priced away from activity — the signal this tracker exists for.",
        "CONGESTION_PRICING": "Regime: CONGESTION_PRICING. REV and activity moving together; Arbitrum's 10% is scaling with throughput.",
        "DECAY": "Regime: DECAY. Both activity and fee-per-gas falling — organic decline, consistent with subsidy roll-off rather than a policy change.",
        "FLAT": "Regime: FLAT. Nothing moved more than 10% against the prior runs.",
        "MIXED": "Regime: MIXED. Activity and pricing moved in directions the classifier doesn't score as a signal.",
        "WARMUP": "Regime: WARMUP. Fewer than two runs of history; classifier not yet active.",
    }.get(regime, regime)
    paras.append(("REV vs activity", body + label))

    # 3. 7d and implied AEP
    aep7 = w7["rev_usd"] * 0.10
    drev7 = pct(w7["rev_usd"], w7p["rev_usd"])
    dfpg7 = pct(w7.get("fee_per_gas_gwei"), w7p.get("fee_per_gas_gwei"))
    paras.append(("Arbitrum's take",
                  f"Trailing 7d REV {usd(w7['rev_usd'])} ({sgn(drev7)} vs prior 7d) implies ≈{usd(aep7)} to the Arbitrum ecosystem "
                  f"at the 10% gross rate (≈{usd(aep7*0.8)} DAO treasury / {usd(aep7*0.2)} Developer Guild), before settlement-cost netting. "
                  f"Fee-per-gas over 7d is {sgn(dfpg7)} vs the prior 7d. "
                  + (f"DefiLlama's 24h chain revenue reads {usd(f(latest.get('llama_rev_24h_usd')))}; Arbitrum One's own sequencer take was {usd(f(latest.get('llama_arb_one_rev_24h_usd')))}. "
                     if latest.get("llama_rev_24h_usd") else "")
                  + ("Robinhood Chain's remittance alone now exceeds Arbitrum One's own daily sequencer surplus."
                     if f(latest.get("llama_rev_24h_usd")) and f(latest.get("llama_arb_one_rev_24h_usd")) and f(latest.get("llama_rev_24h_usd")) * 0.1 > f(latest.get("llama_arb_one_rev_24h_usd")) else "")))

    # 4. calendar
    ds = (SUBSIDY_END - today).days
    cal = (f"Gas-subsidy expiry {SUBSIDY_END.isoformat()} is {ds} days away." if ds >= 0
           else f"Gas subsidy expired {-ds} days ago ({SUBSIDY_END.isoformat()}); the current window is unsubsidised.")
    cal += (f" Nitro BSL change date {CHANGE_DATE.isoformat()} ({(CHANGE_DATE - today).days} days): outer bound on the license-backed 10%.")
    paras.append(("Calendar", cal))

    # 5. what to watch
    paras.append(("What would change the read",
                  "A speed-limit increase or min-base-fee cut on the parameter table; fee-per-gas falling ≥10% while gas/s holds or rises "
                  "(DECOUPLING); implied-AEP-per-gas rolling over on the 7d line while transactions stay elevated; an ArbOS version bump "
                  "(throughput upgrade) followed by fee compression; network_fee_account changing (router swap)."))
    return paras


def md_lite(text):
    """Very small markdown → HTML: paragraphs, **bold**, headings (## ), bullets."""
    out, buf, in_ul = [], [], False

    def flush():
        nonlocal buf
        if buf:
            out.append("<p>" + " ".join(buf) + "</p>")
            buf = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            flush()
            if in_ul:
                out.append("</ul>"); in_ul = False
            continue
        s = esc(s)
        while "**" in s:
            s = s.replace("**", "<b>", 1).replace("**", "</b>", 1)
        if s.startswith("## ") or s.startswith("# "):
            flush()
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h3>{s.lstrip('# ')}</h3>")
        elif s.startswith("- ") or s.startswith("* "):
            flush()
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{s[2:]}</li>")
        else:
            buf.append(s)
    flush()
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


# ----------------------------------------------------------------------
# page
# ----------------------------------------------------------------------

CSS = """
body{background:#141416;color:#e6e6e6;font:14px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:26px;max-width:1040px}
h1{font-size:21px;margin:0} h2{font-size:15px;margin:26px 0 10px;color:#c9c9c9;letter-spacing:.02em;text-transform:uppercase}
h3{font-size:14px;margin:14px 0 4px;color:#e6e6e6}
.sub{color:#8a8a8a;font-size:12px;margin-top:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:8px}
.card{background:#1d1d20;border:1px solid #2a2a2e;border-radius:8px;padding:12px}
.card .k{color:#8a8a8a;font-size:12px} .card .v{font-size:19px;font-weight:600;margin-top:2px} .card .d{color:#8a8a8a;font-size:12px;margin-top:2px}
.regime{display:inline-block;padding:5px 12px;border-radius:6px;font-weight:700;letter-spacing:.03em}
.chart{background:#1d1d20;border:1px solid #2a2a2e;border-radius:8px;padding:12px;margin:10px 0}
.ct{font-size:13px;color:#d0d0d0;margin-bottom:2px} .legend{font-size:11px;color:#9a9a9a;margin-bottom:4px}
.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.svg{display:block;height:230px} .grid{stroke:#2a2a2e;stroke-width:1}
line.grid{stroke:#2a2a2e} .tick{fill:#777;font-size:10px}
.commentary{background:#1d1d20;border:1px solid #2a2a2e;border-left:3px solid #c8ff2e;border-radius:8px;padding:6px 16px}
.commentary p{margin:8px 0} .commentary ul{margin:4px 0 8px 18px}
table{border-collapse:collapse;width:100%} td,th{text-align:left;padding:5px 8px;border-bottom:1px solid #2a2a2e;font-size:13px;vertical-align:top}
th{color:#8a8a8a;font-weight:500} code{color:#c8ff2e;font-size:12px}
.muted{color:#7d7d7d} .small{font-size:11px;margin-top:4px}
.up{color:#c8ff2e} .down{color:#ff6b6b}
.flag{background:#3a1d1d;border:1px solid #ff6b6b;color:#ffb3b3;border-radius:6px;padding:8px 12px;margin:8px 0}
"""


def render(rows, latest, commentary_md=None, out_path="index.html", title="Robinhood Chain → Arbitrum AEP brief"):
    d = daily(rows)
    w24, w24p = window_sum(rows, 0, 24), window_sum(rows, 24, 48)
    w7, w7p = window_sum(rows, 0, 24 * 7), window_sum(rows, 24 * 7, 24 * 14)
    regime = latest.get("regime", "WARMUP")
    rc = {"DECOUPLING": ("#ff6b6b", "#3a1d1d"), "CONGESTION_PRICING": ("#c8ff2e", "#233015"),
          "DECAY": ("#ffb84d", "#3a2d15"), "FLAT": ("#9aa", "#222"), "MIXED": ("#9aa", "#222"), "WARMUP": ("#9aa", "#222")}.get(regime, ("#9aa", "#222"))
    changes = [c for c in (latest.get("param_changes") or "").split(" | ") if c]

    # commentary
    if commentary_md:
        comm_html = md_lite(commentary_md)
        comm_src = "analyst commentary"
    else:
        paras = auto_commentary(rows, latest, d, w24, w24p, w7, w7p)
        comm_html = "".join(f"<h3>{esc(h)}</h3><p>{esc(b)}</p>" for h, b in paras)
        comm_src = "rule-based commentary (no analyst run)"

    # charts
    labels = [x["date"][5:] for x in d]
    c1 = chart("Chain REV per day (USD) and implied AEP 10%", labels,
               [{"name": "REV", "values": [x["rev_usd"] or None for x in d], "color": "#c8ff2e", "kind": "bar", "axis": "l"},
                {"name": "implied AEP", "values": [x["aep_usd"] or None for x in d], "color": "#ffb84d", "kind": "line", "axis": "l"}],
               {"l": "usd"}, "Partial days (first/last) are under-counted; use the 7d cards for totals.")
    c2 = chart("Fee per gas (gwei, left) vs gas per second (right) — the decoupling chart", labels,
               [{"name": "fee/gas gwei", "values": [x["fee_per_gas_gwei"] for x in d], "color": "#ff6b6b", "kind": "line", "axis": "l"},
                {"name": "gas/s", "values": [x["gas_per_sec"] for x in d], "color": "#6cc4ff", "kind": "line", "axis": "r"}],
               {"l": "gwei", "r": "gps"}, "Red falling while blue holds or rises = REV being managed, not lost.")
    util_series = [(x["gas_per_sec"] / x["limit"] * 100) if (x["gas_per_sec"] and x.get("limit")) else None for x in d]
    c3 = chart("Utilisation vs speed limit (%)", labels,
               [{"name": "gas/s ÷ speed limit", "values": util_series, "color": "#6cc4ff", "kind": "bar", "axis": "l"}],
               {"l": "pct"}, "Sustained >100% is where base fee ramps. Uses each day's speed limit; a limit change shows as a step down here and an entry in the parameter log.")
    run_labels = [r["_t"].strftime("%m-%d %Hh") for r in rows[-60:]]
    c4 = chart("Per-run fee per gas (gwei) — last 60 runs", run_labels,
               [{"name": "fee/gas", "values": [f(r.get("fee_per_gas_gwei")) for r in rows[-60:]], "color": "#ff6b6b", "kind": "line", "axis": "l"},
                {"name": "min base fee", "values": [f(r.get("min_base_fee_gwei")) for r in rows[-60:]], "color": "#777", "kind": "line", "axis": "l"}],
               {"l": "gwei"}, "Realized fee/gas converging on the min-base-fee line = chain running uncongested at the floor.")

    # parameter log (all historical changes)
    plog = [(r["_t"].strftime("%Y-%m-%d %H:%M"), r.get("param_changes")) for r in rows if r.get("param_changes")]
    plog_html = "".join(f"<tr><td>{esc(t)}</td><td>{esc(c)}</td></tr>" for t, c in reversed(plog[-30:])) or "<tr><td colspan=2 class='muted'>no parameter changes recorded yet</td></tr>"

    flags = ""
    if changes:
        flags += f"<div class='flag'><b>Parameter change:</b> {esc('; '.join(changes))}</div>"
    if regime == "DECOUPLING":
        flags += "<div class='flag'><b>DECOUPLING:</b> gas up ≥10% and fee/gas down ≥10% vs prior runs.</div>"

    def delta(v):
        if v is None:
            return "<span class='muted'>—</span>"
        return f"<span class='{'up' if v >= 0 else 'down'}'>{v:+.1f}%</span>"

    html = f"""<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title><style>{CSS}</style></head><body>
<h1>{esc(title)}</h1>
<div class='sub'>generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · last data window {esc(latest.get('window_start_utc','')[:16])} → {esc(latest.get('window_end_utc','')[:16])} · {len(rows)} runs in history · {comm_src}</div>
<div style='margin-top:12px'><span class='regime' style='color:{rc[0]};background:{rc[1]};border:1px solid {rc[0]}'>{esc(regime)}</span></div>
{flags}

<h2>Commentary</h2>
<div class='commentary'>{comm_html}</div>

<h2>Numbers</h2>
<div class='grid'>
<div class='card'><div class='k'>REV 24h</div><div class='v'>{usd(w24['rev_usd'])}</div><div class='d'>{delta(pct(w24['rev_usd'], w24p['rev_usd']))} vs prior 24h · {num(w24['rev_eth'],3)} ETH</div></div>
<div class='card'><div class='k'>REV 7d</div><div class='v'>{usd(w7['rev_usd'])}</div><div class='d'>{delta(pct(w7['rev_usd'], w7p['rev_usd']))} vs prior 7d</div></div>
<div class='card'><div class='k'>Implied AEP 7d (10% gross)</div><div class='v'>{usd(w7['rev_usd']*0.1)}</div><div class='d'>DAO {usd(w7['rev_usd']*0.08)} · Guild {usd(w7['rev_usd']*0.02)}</div></div>
<div class='card'><div class='k'>Fee per gas 24h</div><div class='v'>{gw(w24.get('fee_per_gas_gwei'))}</div><div class='d'>{delta(pct(w24.get('fee_per_gas_gwei'), w24p.get('fee_per_gas_gwei')))} vs prior 24h · floor {gw(f(latest.get('min_base_fee_gwei')))}</div></div>
<div class='card'><div class='k'>Gas / second 24h</div><div class='v'>{num(w24.get('gas_per_sec'))}</div><div class='d'>{delta(pct(w24.get('gas_per_sec'), w24p.get('gas_per_sec')))} · limit {num(f(latest.get('speed_limit_gas_per_sec')))}</div></div>
<div class='card'><div class='k'>Fee/gas 7d vs prior 7d</div><div class='v'>{delta(pct(w7.get('fee_per_gas_gwei'), w7p.get('fee_per_gas_gwei')))}</div><div class='d'>gas/s 7d {delta(pct(w7.get('gas_per_sec'), w7p.get('gas_per_sec')))}</div></div>
<div class='card'><div class='k'>DefiLlama 24h rev</div><div class='v'>{usd(f(latest.get('llama_rev_24h_usd')))}</div><div class='d'>Arbitrum One own take {usd(f(latest.get('llama_arb_one_rev_24h_usd')))}</div></div>
<div class='card'><div class='k'>Observed AEP inflow (window)</div><div class='v'>{num(f(latest.get('aep_recipient_inflow_eth')),4) if latest.get('aep_recipient_inflow_eth') not in (None,'') else '—'} ETH</div><div class='d'>needs aep_recipient_address in config</div></div>
</div>

<h2>Charts</h2>
{c1}{c2}{c3}{c4}

<h2>Chain-owner parameters (the dial)</h2>
<table>
<tr><th>parameter</th><th>value</th><th>reading</th></tr>
<tr><td>speed_limit_gas_per_sec</td><td>{num(f(latest.get('speed_limit_gas_per_sec')))}</td><td>raise → congestion pricing dies at the same activity</td></tr>
<tr><td>min_base_fee</td><td>{gw(f(latest.get('min_base_fee_gwei')))}</td><td>REV/gas floor when uncongested</td></tr>
<tr><td>pricing_inertia / backlog_tolerance</td><td>{esc(latest.get('pricing_inertia'))} / {esc(latest.get('backlog_tolerance'))}</td><td>ramp speed and how much backlog is free</td></tr>
<tr><td>gas_pool_max / max_tx_gas_limit</td><td>{num(f(latest.get('gas_pool_max')))} / {num(f(latest.get('max_tx_gas_limit')))}</td><td>burst capacity</td></tr>
<tr><td>arbos_version</td><td>v{esc(latest.get('arbos_version'))}</td><td>throughput upgrades change the REV↔activity map</td></tr>
<tr><td>network_fee_account</td><td><code>{esc(latest.get('network_fee_account'))}</code></td><td>should be the AEP RewardDistributor</td></tr>
<tr><td>infra_fee_account</td><td><code>{esc(latest.get('infra_fee_account'))}</code></td><td>min-base-fee component recipient</td></tr>
<tr><td>l1_reward_rate → recipient</td><td>{esc(latest.get('l1_reward_rate'))} → <code>{esc(latest.get('l1_reward_recipient'))}</code></td><td>owner's L1-pricing reward</td></tr>
<tr><td>state: l2 base fee now / L1 est / backlog / L1 surplus</td><td>{gw(f(latest.get('l2_base_fee_now_gwei')))} / {gw(f(latest.get('l1_base_fee_est_gwei')))} / {num(f(latest.get('gas_backlog')))} / {num(f(latest.get('l1_pricing_surplus_eth')),3)} ETH</td><td>state, not policy</td></tr>
</table>

<h2>Parameter change log</h2>
<table><tr><th>run</th><th>change</th></tr>{plog_html}</table>

<div class='muted small' style='margin-top:20px'>Method: REV = Σ baseFee×gasUsed per block via eth_feeHistory (Nitro FCFS, no tips). Implied AEP = 10% × gross REV, before settlement/DA netting → upper bound. Parameters read from ArbGasInfo (0x6C), ArbOwnerPublic (0x6b), ArbSys (0x64). Regime: ±10% thresholds vs the mean of the prior runs. Self-contained file; no external requests.</div>
</body></html>"""
    with open(out_path, "w") as fh:
        fh.write(html)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--latest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--commentary", default=None, help="markdown file with analyst commentary")
    ap.add_argument("--title", default="Robinhood Chain → Arbitrum AEP brief")
    a = ap.parse_args()
    rows, latest = load(a.history, a.latest)
    comm = open(a.commentary).read() if a.commentary else None
    print(render(rows, latest, comm, a.out, a.title))


if __name__ == "__main__":
    main()
