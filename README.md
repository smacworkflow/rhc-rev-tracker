# Robinhood Chain → Arbitrum AEP tracker

Leading indicators that Robinhood is dialing chain REV down relative to activity
(and therefore that Arbitrum DAO's 10% AEP take is being capped):

1. **Policy parameters the chain owner controls** — read directly from the Nitro
   precompiles (`ArbGasInfo` 0x6C, `ArbOwnerPublic` 0x6b, `ArbSys` 0x64): speed limit,
   minimum base fee, pricing inertia, backlog tolerance, L1 reward rate/recipient,
   fee accounts, ArbOS version. Any change between runs is flagged. This is the
   earliest possible signal — REV cannot be decoupled from activity on this stack
   without one of these moving (or an ArbOS throughput upgrade, also captured).
2. **Realized fee-per-gas vs gas consumed** — reconstructed block-by-block from
   `eth_feeHistory` (Σ baseFee × gasUsed; Nitro is FCFS with no tips, so base fee is
   the whole sequencer revenue). Regime classifier:
   - `DECOUPLING` — gas up ≥10%, fee/gas down ≥10% vs the prior N runs → REV managed, not lost
   - `CONGESTION_PRICING` — both up (current state)
   - `DECAY` — both down (organic post-subsidy decline)
   - `FLAT` / `MIXED`
   Implied AEP = 10% × gross REV (upper bound; the contract nets settlement/DA costs).

Cross-checks: DefiLlama 24h chain revenue (Robinhood Chain and Arbitrum One's own),
and optional observed ETH inflow to the AEP recipient address via Blockscout.

## Setup

1. Create a repo, copy these files in, push.
2. Settings → Pages → deploy from branch `main`, folder `/docs`. The dashboard is
   `docs/index.html`; `docs/latest.md` is Obsidian-flavored for your vault.
3. Actions tab → run `rhc-aep-tracker` once manually (workflow_dispatch) to seed.
   It then runs every 12h; edit the cron in `.github/workflows/tracker.yml` for 24h.

## Config (`config.json`)

- `rpc_urls`: list; falls over on error. Public RPC is rate-limited — add a
  QuickNode/Alchemy/Chainstack endpoint if runs fail. A 12h window is ~170 `eth_feeHistory`
  calls + ~40 misc calls.
- `window_hours`: first-run window; subsequent runs are contiguous from the last end block
  (capped by `max_window_hours`) so history sums cleanly.
- `defillama_chain_slugs`: tried in order; verify the slug at defillama.com/chain/robinhood-chain.
- `aep_recipient_address`: set to the 10% recipient of the RewardDistributor at the
  `network_fee_account` (visible on Blockscout) to log observed remittance instead of implied.
- `trend_runs`: how many prior runs the regime classifier averages over.

## Outputs

- `data/history.csv` — one row per run, all metrics + parameters (feed it into anything)
- `data/latest.json` — last run snapshot (used for change detection)
- `docs/index.html` — self-contained dashboard (no external requests)
- `docs/latest.md` — vault note with frontmatter

## Reading it

Sequence to expect around the Sept 29 subsidy expiry: sponsored share of gas falls →
activity drop (`DECAY`) → Robinhood raises `speed_limit_gas_per_sec` or cuts
`min_base_fee_gwei` to retain traders → gas stabilises while fee/gas compresses
(`DECOUPLING`) → implied AEP per unit gas rolls over. The parameter flags fire days to
weeks before the REV rollover is visible in headline numbers.
