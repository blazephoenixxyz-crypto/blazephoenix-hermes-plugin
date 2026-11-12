"""BlazePhoenix for Hermes: swap quotes measured on your own node, verified before anything is signed."""

from __future__ import annotations

import json

try:
    from . import bp
except ImportError:  # imported standalone by a test runner
    import bp

_CHAIN = {"type": "string", "description": "base (default) | eth | optimism | arbitrum | robinhood, or a chain id"}
_TOKEN = "a 0x token address, or one of the symbols WETH, USDC (not on Robinhood), BZPX (Base only) or USDG (Robinhood only). Never guess an address for another ticker: ask the user."
_ADDR = {"type": "string", "description": "0x address"}

QUOTE = {
    "name": "blazephoenix_quote",
    "description": (
        "Quote a swap on BlazePhoenix, the on-chain DEX aggregator. The quote is an eth_call to the BlazePhoenix "
        "Quoter on the user's OWN RPC (BLAZEPHOENIX_RPC_<CHAIN>), decoded and cross-checked locally. Returns the net "
        "output after the fee, the on-chain minimum, price impact and the Phoenix Check verdict (ok / caution / "
        "danger / blocked; respect blocked). With recipient it also returns UNSIGNED Router calldata that has already "
        "been verified field by field (router, amount, recipient, minimum, deadline, route) and only when one can be built; its 'verification' field is "
        "that check, so do not re-run it by copying the calldata. Native ETH must be wrapped to WETH first. It never "
        "signs or sends."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chain": _CHAIN,
            "token_in": {"type": "string", "description": "input token: " + _TOKEN},
            "token_out": {"type": "string", "description": "output token: " + _TOKEN},
            "amount_in": {"type": "string", "description": "input amount in base units (integer string, e.g. 1000000000000000000 for 1 WETH)"},
            "recipient": {**_ADDR, "description": "optional: who receives token_out; adds verified unsigned calldata"},
            "min_out": {"type": "string", "description": "optional: least output accepted, base units; default is the quote minus slippage_bps"},
            "slippage_bps": {"type": "integer", "minimum": 0, "maximum": 5000, "description": "default 50"},
        },
        "required": ["token_in", "token_out", "amount_in"],
        "additionalProperties": False,
    },
}

VERIFY = {
    "name": "blazephoenix_verify_calldata",
    "description": (
        "Check unsigned BlazePhoenix Router calldata that came from somewhere else (another agent, an app, a pasted "
        "transaction) before it is signed; blazephoenix_quote already verifies its own. Pure and offline: decodes the "
        "swapExactIn call and refuses it unless the target is the pinned Router, value is 0, the encoding is canonical, "
        "the amount and recipient equal what the user asked for, the minimum output is not below it, the deadline is "
        "within the next hour and the route connects token_in to token_out."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chain": _CHAIN,
            "to": _ADDR,
            "data": {"type": "string", "description": "0x calldata"},
            "value": {"type": "string", "description": "transaction value in wei, default 0"},
            "token_in": {"type": "string", "description": _TOKEN},
            "token_out": {"type": "string", "description": _TOKEN},
            "amount_in": {"type": "string", "description": "base units"},
            "recipient": _ADDR,
            "min_out": {"type": "string", "description": "least output the user accepts, base units"},
        },
        "required": ["to", "data", "token_in", "token_out", "amount_in", "recipient", "min_out"],
        "additionalProperties": False,
    },
}

CHAINS = {
    "name": "blazephoenix_chains",
    "description": "List the chains BlazePhoenix quotes on, the pinned Router and Quoter addresses, known token symbols, "
                   "and whether an RPC is configured for each (the URL itself is never shown).",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _run(fn) -> str:
    try:
        return json.dumps({"ok": True, "result": fn()})
    except bp.BPError as e:
        return json.dumps({"ok": False, "code": e.code, "error": str(e), **({"details": e.details} if e.details else {})})
    except Exception as e:  # handlers never raise; no URL is ever echoed (bp scrubs network errors)
        return json.dumps({"ok": False, "code": "error", "error": type(e).__name__})


def quote(args: dict, **kwargs) -> str:
    return _run(lambda: bp.quote(args.get("chain"), args.get("token_in", ""), args.get("token_out", ""),
                                 args.get("amount_in", ""), args.get("recipient"), args.get("min_out"),
                                 args.get("slippage_bps", 50)))


def verify_calldata(args: dict, **kwargs) -> str:
    def go():
        cid = bp.resolve_chain(args.get("chain"))
        return bp.verify_swap(cid, {"to": args.get("to", ""), "data": args.get("data", ""), "value": args.get("value", "0")},
                              token_in=bp.resolve_token(cid, args.get("token_in", "")),
                              token_out=bp.resolve_token(cid, args.get("token_out", "")),
                              amount_in=bp._uint(args.get("amount_in", ""), "amount_in"),
                              recipient=bp._addr(args.get("recipient", ""), "recipient"),
                              min_out=bp._uint(args.get("min_out", ""), "min_out"))
    return _run(go)


def chains(args: dict, **kwargs) -> str:
    return _run(bp.chains)


def register(ctx):
    ctx.register_tool(name=QUOTE["name"], toolset="blazephoenix", schema=QUOTE, handler=quote)
    ctx.register_tool(name=VERIFY["name"], toolset="blazephoenix", schema=VERIFY, handler=verify_calldata)
    ctx.register_tool(name=CHAINS["name"], toolset="blazephoenix", schema=CHAINS, handler=chains)
