# BlazePhoenix for Hermes Agent

Swap quotes from [BlazePhoenix](https://blazephoenix.xyz), the fully on-chain DEX aggregator, measured on **your own node** and verified locally before anything is signed.

- The quote is an `eth_call` to the BlazePhoenix Quoter, run on the RPC URL you configure. BlazePhoenix never sees your node and runs no RPC for you.
- The plugin decodes the raw result itself and refuses a hosted decode whose net output, gross output or tokens differ from what your node returned.
- Unsigned Router calldata is checked field by field (router address, canonical encoding, amount, recipient, minimum output, deadline, approval, and the route your node previewed) before it is shown as ready to sign.
- It never signs, never sends a transaction and never handles a private key. Standard library only, no Python dependencies.

## Tools

| Tool | What it does | Network |
|---|---|---|
| `blazephoenix_quote` | Net output after the fee, on-chain minimum, price impact, Phoenix Check verdict (`ok` / `caution` / `danger` / `blocked`). With `recipient`, also verified unsigned calldata and the exact ERC-20 approval, only when your node measures the route as executable (`canExecute`). When the plugin withholds a hosted transaction for that reason, it returns `txError` `not_executable`; otherwise any `txError` is the hosted one. | your RPC + blazephoenix.xyz |
| `blazephoenix_verify_calldata` | Checks `swapExactIn` calldata from any source: pinned Router, canonical encoding, `value` 0, and amount and recipient equal to what the user asked for, minimum output not below it, deadline within the next hour, route connecting the two tokens. Pure and offline. | none |
| `blazephoenix_usd_value` | What an amount of a token is worth in the chain's dollar token (USDC; USDG on Robinhood Chain), which is taken as exactly 1 USD. The pinned Quoter (`previewPlan`) prices the token directly and through WETH and through the tokens its direct route touches (at most 8 vias, each at most 2 token hops), all inside one Multicall3 `aggregate3` eth_call pinned to one block hash (a second call at the same hash only when the route adds tokens; a lost hash restarts once, then `not measured`). The value is the least of the measured vias: the net output actually received for the amount after the 0.28% fee, including price impact; a leg X -> dollar is priced at the amount of X the route moves (`scaledFrom`) and scaled linearly, so its impact is the impact at that amount. A via that fails, routes nothing or returns 0 is `not measured`, never 0; with no measured via the value is `not measured`. Returns the label (e.g. `USD (via USDC)`), block, every via with its legs, and the spread between vias (`bps`), with no threshold or verdict. | your RPC only |
| `blazephoenix_chains` | Chains, pinned Router and Quoter addresses, token symbols, and whether an RPC is set (the URL is never shown). | none |

Chains: Base, Ethereum, Optimism, Arbitrum, Robinhood Chain (protocol 1.0.0). Amounts are in base units. Native ETH: wrap to WETH first (the 1.0.0 Router trades ERC-20 tokens).

## Setup

```bash
hermes plugins install blazephoenix
# prompted for BLAZEPHOENIX_RPC_BASE: your own Base node URL (any provider)
```

Optional, one per chain: `BLAZEPHOENIX_RPC_ETHEREUM`, `BLAZEPHOENIX_RPC_OPTIMISM`, `BLAZEPHOENIX_RPC_ARBITRUM`, `BLAZEPHOENIX_RPC_ROBINHOOD`. A chain without an RPC answers `rpc_required`; nothing falls back to a shared node. The URL is read through Hermes's secret scope and never appears in tool output or errors.

Then ask, for example: *"Quote 0.1 WETH to USDC on Base for 0x… and verify the calldata before I sign."*

## How a quote is made

1. The plugin asks your node for `eth_chainId` and refuses a node that serves another chain.
2. `GET https://blazephoenix.xyz/api/quote/prepare` returns the request (deadline, slippage) and the `eth_call` it expects. The plugin re-encodes that call itself and refuses any difference in target, bytes, request fields or Router address. The Router and Quoter addresses are pinned in `bp.py`; a new deployment reaches you only through a reviewed release of this plugin.
3. Your node runs `previewPlanWithMinOut` on the pinned Quoter.
4. `POST https://blazephoenix.xyz/api/quote/decode` (pure, no RPC) returns the quote, the Phoenix Check and the unsigned calldata.
5. The plugin decodes your node's result locally and refuses a hosted decode whose net output, gross output or tokens differ from it. It recomputes the Phoenix Check itself: the verdict returned is the local one, and `phoenixCheck.agree` reports whether the hosted verdict matches. The calldata is verified against the request and your node's route.

The Router also enforces the minimum output on-chain: a worse fill reverts.

## What the plugin does on your machine

- Network: HTTPS to `blazephoenix.xyz` (prepare and decode: token addresses, amount, recipient, minimum; never your RPC URL) and JSON-RPC `eth_chainId` / `eth_call` to the RPC URL you set (`blazephoenix_usd_value` calls the Quoter through Multicall3 `0xcA11bde05977b3631167028862bE2a173976CA11` on your node and nothing else). Nothing else.
- No shell commands, no background processes, no files read or written, no telemetry, no stored credentials beyond the RPC URL you give Hermes.

## Tests

```bash
python -m pytest            # 415 offline tests: ABI codec vs eth-abi, selectors, verification, Phoenix Check, USD value
BLAZEPHOENIX_RPC_BASE=<your Base node> python -m pytest   # + 11 live tests on Base, including a lying-server suite
python tests/mutants.py     # 47 code mutants, each must turn the suite red
```

Offline tests need the test-only pins in `tests/requirements.txt` (`pytest`, plus `eth-abi` and `pycryptodome` for the differential codec and selector tests, which skip when they are missing). The Hermes host module `agent.secret_scope` is stubbed in `tests/conftest.py` when no host is installed.

## Links

- Site: https://blazephoenix.xyz
- SDK (MIT): https://github.com/blazephoenixxyz-crypto/SDK
- MCP server (MIT): https://github.com/blazephoenixxyz-crypto/blazephoenix-mcp
- Author: Mitra ([x.com/sigmacrit](https://x.com/sigmacrit)) · contact@blazephoenix.xyz

MIT License.
