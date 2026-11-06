"""BlazePhoenix quotes for Hermes: the eth_call runs on YOUR node, everything is verified locally.

Flow of one quote:
  1. prepare  (blazephoenix.xyz, pure, no RPC): the request object (deadline, slippage) and the
     eth_call it expects. The plugin re-encodes that eth_call itself and refuses any difference.
  2. eth_call on the RPC from BLAZEPHOENIX_RPC_<CHAIN> to the Quoter pinned in this file.
  3. decode   (blazephoenix.xyz, pure, no RPC): the quote, the Phoenix Check and unsigned calldata.
  4. The plugin decodes the raw eth_call result itself, refuses a decode that disagrees with it,
     and verifies the calldata field by field against the request and the route its own node
     returned, before anything is shown as ready to sign.

Nothing here signs, sends a transaction or touches a private key. Standard library only.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

API = "https://blazephoenix.xyz"
UA = "blazephoenix-hermes/1.0.0"
TIMEOUT = 20

# Protocol 1.0.0 deployments (@blazephoenix/sdk src/deployments.ts and src/constants.ts).
# A new deployment reaches users only through a reviewed release of this plugin.
CHAINS = {
    8453: dict(key="BASE", name="Base", router="0x2a779f9Be49aac57495A8B6467Cc325a8a47Eb9f",
               quoter="0x4cEF0615614B212895F45Aa1D4833B16666E18d3",
               tokens=dict(WETH="0x4200000000000000000000000000000000000006",
                           USDC="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                           BZPX="0x053D75c00FdD571538492e3B1686Bd963a0911A7")),
    1: dict(key="ETHEREUM", name="Ethereum", router="0xE1aE5f49013920CF71De8CED4043e14C4d63416b",
            quoter="0x4a20AA0912388ff7A9221Ab6BFC224cc20Baa0c3",
            tokens=dict(WETH="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                        USDC="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")),
    10: dict(key="OPTIMISM", name="Optimism", router="0x7262e7483ab6f0db7b8f90eC3a9de3B02Ab36F6A",
             quoter="0xfB18EF6f62A0278A273Af4b7A46b454F9E482dc2",
             tokens=dict(WETH="0x4200000000000000000000000000000000000006",
                         USDC="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85")),
    42161: dict(key="ARBITRUM", name="Arbitrum", router="0x7262e7483ab6f0db7b8f90eC3a9de3B02Ab36F6A",
                quoter="0xfB18EF6f62A0278A273Af4b7A46b454F9E482dc2",
                tokens=dict(WETH="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
                            USDC="0xaf88d065e77c8cC2239327C5EDb3A432268e5831")),
    4663: dict(key="ROBINHOOD", name="Robinhood Chain", router="0x7262e7483ab6f0db7b8f90eC3a9de3B02Ab36F6A",
               quoter="0xE1aE5f49013920CF71De8CED4043e14C4d63416b",
               tokens=dict(WETH="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
                           USDG="0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")),
}
ALIASES = {"base": 8453, "eth": 1, "ethereum": 1, "mainnet": 1, "op": 10, "optimism": 10,
           "arb": 42161, "arbitrum": 42161, "robinhood": 4663, "rh": 4663}

SEL_PREVIEW = "a4c29e06"   # previewPlanWithMinOut(address,address,uint256,uint256)
SEL_SWAP = "36457c51"      # swapExactIn(Route,uint256,uint256,address,uint256)
HARD_IMPACT_BPS, CAUTION_IMPACT_BPS = 2000, 200   # @blazephoenix/sdk src/checks.ts
MAX_DEADLINE_SEC = 3600

# ABI types: a scalar is a string, a tuple a list, a dynamic array a 1-tuple (element,).
LEG = ["address", "address", "uint8", "uint24", "int24", "bool", "bool", "uint256", "uint256", "bytes32"]
HOP = ["address", "address", "uint256", "uint256", (LEG,)]
ROUTE = [(HOP,), "uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "bool", "bool"]
PREVIEW = [ROUTE] + ["uint256"] * 10 + ["uint8", "address", "bool"]
PREVIEW_OUT = [PREVIEW, ROUTE, "bool"]
SWAP_IN = [ROUTE, "uint256", "uint256", "address", "uint256"]
PV_FIELDS = ["route", "grossOut", "protocolFee", "safetyBuffer", "netOut", "ironFloor", "userMinOut",
             "effectiveMinOut", "estGas", "hops", "legs", "topology", "bridgeUsed", "canExecute"]


class BPError(Exception):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code, self.details = code, details


# ---- minimal strict ABI codec ------------------------------------------------------------------

def _dyn(t) -> bool:
    return isinstance(t, tuple) or (isinstance(t, list) and any(_dyn(x) for x in t))


def _head(t) -> int:
    return 32 if _dyn(t) or isinstance(t, str) else sum(_head(x) for x in t)


def _word(d: bytes, o: int) -> int:
    if o < 0 or o + 32 > len(d):
        raise BPError("abi", "ABI data too short")
    return int.from_bytes(d[o:o + 32], "big")


def _scalar(t: str, w: int):
    if t == "address":
        if w >> 160:
            raise BPError("abi", "dirty address word")
        return "0x%040x" % w
    if t == "bool":
        if w > 1:
            raise BPError("abi", "dirty bool word")
        return bool(w)
    if t == "bytes32":
        return "0x%064x" % w
    bits = int(t.lstrip("uint") or 256)
    if t.startswith("uint"):
        if w >> bits:
            raise BPError("abi", f"{t} out of range")
        return w
    v = w - (1 << 256) if w >> 255 else w
    if not -(1 << (bits - 1)) <= v < (1 << (bits - 1)):
        raise BPError("abi", f"{t} out of range")
    return v


def _dec(t, d: bytes, o: int):
    if isinstance(t, str):
        return _scalar(t, _word(d, o))
    if isinstance(t, tuple):
        n = _word(d, o)
        if n > len(d) // 32:
            raise BPError("abi", "array length out of range")
        return [_dec(t[0], d, o + 32 + i * _head(t[0])) if not _dyn(t[0])
                else _dec(t[0], d, o + 32 + _word(d, o + 32 + 32 * i)) for i in range(n)]
    out, p = [], o
    for x in t:
        out.append(_dec(x, d, o + _word(d, p)) if _dyn(x) else _dec(x, d, p))
        p += _head(x)
    return out


def _enc(t, v) -> bytes:
    if isinstance(t, str):
        if t == "address":
            v = int(v, 16)
        elif t == "bytes32":
            v = int(v, 16)
        return (int(v) % (1 << 256)).to_bytes(32, "big")
    if isinstance(t, tuple):
        return len(v).to_bytes(32, "big") + _enc([t[0]] * len(v), v)
    heads, tails, size = [], [], sum(_head(x) for x in t)
    for x, y in zip(t, v):
        if _dyn(x):
            heads.append((size + sum(map(len, tails))).to_bytes(32, "big"))
            tails.append(_enc(x, y))
        else:
            heads.append(_enc(x, y))
    return b"".join(heads + tails)


def abi_decode(types: list, data: bytes, strict: bool = True):
    """Decode a tuple; with strict=True the bytes must be exactly the canonical encoding."""
    v = _dec(types, data, 0)
    if strict and _enc(types, v) != data:
        raise BPError("abi", "non-canonical ABI encoding")
    return v


def _hex(s: str) -> bytes:
    if not isinstance(s, str) or not s.startswith("0x"):
        raise BPError("bad_request", "expected 0x-prefixed hex")
    return bytes.fromhex(s[2:])


# ---- inputs ------------------------------------------------------------------------------------

def resolve_chain(chain) -> int:
    s = str(chain if chain is not None else "base").strip().lower()
    cid = int(s) if s.isdigit() else ALIASES.get(s)
    if cid not in CHAINS:
        raise BPError("bad_request", f"unsupported chain {chain!r}: use base, eth, optimism, arbitrum or robinhood")
    return cid


def _addr(a: str, what: str) -> str:
    if not (isinstance(a, str) and len(a) == 42 and a.startswith("0x")):
        raise BPError("bad_request", f"{what}: expected a 0x address (40 hex characters)")
    int(a, 16)
    return a.lower()


def resolve_token(cid: int, t: str) -> str:
    s = str(t).strip()
    if s.upper() == "ETH":
        raise BPError("wrap_required", "the 1.x Router trades ERC-20 tokens: wrap ETH to WETH and quote WETH")
    sym = CHAINS[cid]["tokens"].get(s.upper())
    return (sym or _addr(s, "token")).lower()


def _uint(v, what: str) -> int:
    try:
        n = int(str(v), 10)
    except ValueError:
        raise BPError("bad_request", f"{what}: expected an integer in base units") from None
    if not 0 < n < 1 << 256:
        raise BPError("bad_request", f"{what}: must be positive")
    return n


# ---- network -----------------------------------------------------------------------------------

def _http(url: str, body=None, secret: str | None = None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "user-agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read(4_000_000))
    except Exception as e:  # never echo a URL: an RPC URL may carry a key
        msg = str(getattr(e, "code", "") or type(e).__name__)
        raise BPError("network", f"request to {'your RPC' if secret else urllib.parse.urlsplit(url).netloc} failed ({msg})") from None


def rpc_url(cid: int) -> str:
    from agent.secret_scope import get_secret
    name = f"BLAZEPHOENIX_RPC_{CHAINS[cid]['key']}"
    url = (get_secret(name) or "").strip()
    if not url.startswith(("https://", "http://")):
        raise BPError("rpc_required", f"set {name} to your own node URL for {CHAINS[cid]['name']} (any provider)")
    return url


def rpc(url: str, method: str, params: list):
    r = _http(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, secret=url)
    if not isinstance(r, dict) or "result" not in r:
        err = (r or {}).get("error", {}) if isinstance(r, dict) else {}
        raise BPError("rpc_error", f"your RPC answered an error: {str(err.get('message', 'no result'))[:200]}")
    return r["result"]


# ---- checks ------------------------------------------------------------------------------------

def route_consistent(route, tin: str, tout: str) -> bool:
    hops = route[0]
    return (len(hops) > 0 and hops[0][0] == tin and hops[-1][1] == tout
            and all(hops[i][0] == hops[i - 1][1] for i in range(1, len(hops)))
            and all(len(h[4]) > 0 for h in hops))


def phoenix_check(pv: dict, tin: str, tout: str) -> str:
    impact = min(10_000, pv["route"][4])
    if not pv["canExecute"] or not route_consistent(pv["route"], tin, tout):
        return "blocked"
    return "danger" if impact >= HARD_IMPACT_BPS else "caution" if impact >= CAUTION_IMPACT_BPS else "ok"


def verify_swap(cid: int, tx: dict, *, token_in: str, token_out: str, amount_in: int, recipient: str,
                min_out: int, deadline: int | None = None, route=None, approval: dict | None = None,
                now: int | None = None) -> dict:
    """Pure check of unsigned Router calldata against what the user asked for. Raises calldata_mismatch."""
    c, bad = CHAINS[cid], []
    tin, tout, rcp = token_in.lower(), token_out.lower(), recipient.lower()
    if str(tx.get("to", "")).lower() != c["router"].lower():
        bad.append(f"to {tx.get('to')} is not the {c['name']} Router {c['router']}")
    if int(str(tx.get("value", "0")), 0) != 0:
        bad.append("value must be 0 for swapExactIn")
    data = _hex(tx.get("data", ""))
    if data[:4].hex() != SEL_SWAP:
        raise BPError("calldata_mismatch", f"not swapExactIn calldata (selector {data[:4].hex()})")
    try:
        r, amt, mn, to, dl = abi_decode(SWAP_IN, data[4:])
    except BPError as e:
        raise BPError("calldata_mismatch", f"calldata does not decode as swapExactIn: {e}") from None
    now = int(time.time()) if now is None else now
    if amt != amount_in:
        bad.append(f"amountIn {amt} != {amount_in}")
    if to != rcp:
        bad.append(f"recipient {to} != {rcp}")
    if mn < max(min_out, 1):
        bad.append(f"userMinOut {mn} < required {max(min_out, 1)}")
    if deadline is not None and dl != deadline:
        bad.append(f"deadline {dl} != {deadline}")
    if not now < dl <= now + MAX_DEADLINE_SEC:
        bad.append(f"deadline {dl} is not within the next {MAX_DEADLINE_SEC} s")
    if not route_consistent(r, tin, tout):
        bad.append("route does not connect tokenIn to tokenOut")
    if route is not None and r != route:
        bad.append("encoded route differs from the route your node returned")
    if approval is not None and (str(approval.get("token", "")).lower() != tin
                                 or str(approval.get("spender", "")).lower() != c["router"].lower()
                                 or int(str(approval.get("amount", "0"))) != amount_in):
        bad.append("approval is not exactly amountIn of tokenIn to the Router")
    if bad:
        raise BPError("calldata_mismatch", "calldata refused, it does not execute what was asked: " + "; ".join(bad),
                      {"mismatches": bad})
    return {"ok": True, "function": "swapExactIn", "router": c["router"], "amountIn": str(amt),
            "userMinOut": str(mn), "recipient": to, "deadline": dl, "hops": len(r[0]),
            "checked": ["router address", "selector", "canonical encoding", "amountIn", "recipient",
                        "userMinOut", "deadline", "route connects tokenIn to tokenOut", "value"]
            + (["route equals your node's preview"] if route is not None else [])
            + (["exact approval"] if approval is not None else [])}


# ---- quote -------------------------------------------------------------------------------------

def _round(cid: int, url: str, tin: str, tout: str, amt: int, min_out: int, recipient, slip: int):
    c = CHAINS[cid]
    q = {"chain": cid, "in": tin, "out": tout, "amountIn": amt, "userMinOut": min_out, "version": 1,
         "slippageBps": slip}
    if recipient:
        q["recipient"] = recipient
    prep = _http(f"{API}/api/quote/prepare?{urllib.parse.urlencode(q)}")
    if not prep.get("ok"):
        raise BPError(prep.get("code", "prepare_failed"), str(prep.get("error", "prepare failed"))[:300])
    req = prep.get("request") or {}
    call = prep.get("call") or {}
    data = "0x" + SEL_PREVIEW + _enc(["address", "address", "uint256", "uint256"], [tin, tout, amt, min_out]).hex()
    want = {"chainId": cid, "tokenIn": tin, "tokenOut": tout, "amountIn": str(amt), "userMinOut": str(min_out)}
    got = {k: (str(req.get(k, "")).lower() if k.startswith("token") else req.get(k) if k == "chainId" else str(req.get(k)))
           for k in want}
    if (got != want or str(call.get("to", "")).lower() != c["quoter"].lower() or call.get("data") != data
            or str((prep.get("contracts") or {}).get("router", "")).lower() != c["router"].lower()):
        raise BPError("prepare_mismatch", "prepare does not match the pinned Quoter/Router and your request: refused")
    raw = rpc(url, "eth_call", [{"to": c["quoter"], "data": data}, "latest"])
    pv_t, _fallback, _has = abi_decode(PREVIEW_OUT, _hex(raw), strict=False)
    pv = dict(zip(PV_FIELDS, pv_t))
    dec = _http(f"{API}/api/quote/decode", {"request": req, "result": raw})
    if not dec.get("ok"):
        raise BPError(dec.get("code", "decode_failed"), str(dec.get("error", "decode failed"))[:300])
    if (str(dec.get("amountOut")) != str(pv["netOut"]) or str(dec.get("grossAmountOut")) != str(pv["grossOut"])
            or str(dec.get("tokenIn", "")).lower() != tin or str(dec.get("tokenOut", "")).lower() != tout):
        raise BPError("decode_mismatch", "the hosted decode disagrees with the result your node returned: refused")
    return req, pv, dec


def quote(chain, token_in: str, token_out: str, amount_in, recipient: str | None = None,
          min_out=None, slippage_bps: int = 50) -> dict:
    cid = resolve_chain(chain)
    tin, tout, amt = resolve_token(cid, token_in), resolve_token(cid, token_out), _uint(amount_in, "amount_in")
    rcp = _addr(recipient, "recipient") if recipient else None
    slip = int(slippage_bps)
    if not 0 <= slip <= 5000:
        raise BPError("bad_request", "slippage_bps must be 0-5000")
    mn = _uint(min_out, "min_out") if min_out not in (None, "") else None
    url = rpc_url(cid)
    if int(rpc(url, "eth_chainId", []), 16) != cid:
        raise BPError("rpc_wrong_chain", f"your RPC for {CHAINS[cid]['name']} serves another chain")
    req, pv, dec = _round(cid, url, tin, tout, amt, mn or 1, rcp, slip)
    if rcp and mn is None and pv["canExecute"]:
        mn = max(pv["netOut"] - pv["netOut"] * slip // 10_000, 1)   # same rounding as the SDK minOutFor
        req, pv, dec = _round(cid, url, tin, tout, amt, mn, rcp, slip)
    local = phoenix_check(pv, tin, tout)
    out = {
        "chain": CHAINS[cid]["name"], "chainId": cid, "tokenIn": tin, "tokenOut": tout, "amountIn": str(amt),
        "amountOut": str(pv["netOut"]), "grossOut": str(pv["grossOut"]), "protocolFee": str(pv["protocolFee"]),
        "ironFloor": str(pv["ironFloor"]), "effectiveMinOut": str(pv["effectiveMinOut"]),
        "impactBps": pv["route"][4], "hops": pv["hops"], "legs": pv["legs"], "canExecute": pv["canExecute"],
        "phoenixCheck": {"verdict": local, "hostedVerdict": (dec.get("checks") or {}).get("verdict"),
                         "agree": local == (dec.get("checks") or {}).get("verdict")},
        "measuredOn": f"your RPC (BLAZEPHOENIX_RPC_{CHAINS[cid]['key']}), Quoter {CHAINS[cid]['quoter']}",
    }
    if dec.get("tx"):
        out["verification"] = verify_swap(cid, dec["tx"], token_in=tin, token_out=tout, amount_in=amt,
                                          recipient=rcp, min_out=mn, deadline=int(req["deadline"]),
                                          route=pv["route"], approval=dec.get("approval"))
        out["approval"] = dec.get("approval")
        out["unsignedTx"] = {**dec["tx"], "chainId": cid}
        out["note"] = "Unsigned. Review it, then sign in your own wallet; this plugin never signs or sends."
    elif dec.get("txError"):
        out["txError"] = dec["txError"]
    return out


def chains() -> list:
    from agent.secret_scope import get_secret
    return [{"chain": c["name"], "chainId": cid, "rpcEnv": f"BLAZEPHOENIX_RPC_{c['key']}",
             "rpcConfigured": bool((get_secret(f"BLAZEPHOENIX_RPC_{c['key']}") or "").strip()),
             "router": c["router"], "quoter": c["quoter"], "tokens": c["tokens"], "protocolVersion": "1.0.0"}
            for cid, c in CHAINS.items()]
