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

import base64
import json
import re
import time
import unicodedata
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
                           USDC="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")),
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
    return t == "bytes" or isinstance(t, tuple) or (isinstance(t, list) and any(_dyn(x) for x in t))


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
    if t == "bytes":
        n = _word(d, o)
        if n > len(d) - o - 32:
            raise BPError("abi", "bytes length out of range")
        return "0x" + d[o + 32:o + 32 + n].hex()
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
    if t == "bytes":
        b = _hex(v)
        return len(b).to_bytes(32, "big") + b + bytes(-len(b) % 32)
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
    if s.upper() == "BZPX":
        raise BPError("bad_request", "BZPX: coming soon — the token address is not published yet; pass a token address")
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


def _rpc_env(cid: int) -> str:
    return f"BLAZEPHOENIX_RPC_{CHAINS[cid]['key']}"


def rpc_url(cid: int) -> str:
    from agent.secret_scope import get_secret
    name = _rpc_env(cid)
    url = (get_secret(name) or "").strip()
    if not url.startswith(("https://", "http://")):
        raise BPError("rpc_required", f"set {name} to your own node URL for {CHAINS[cid]['name']} (any provider)")
    return url


def _secrets() -> list:
    """Every RPC URL configured, one per chain that has one (the same names rpc_url reads)."""
    from agent.secret_scope import get_secret
    return [u for u in ((get_secret(_rpc_env(cid)) or "").strip() for cid in CHAINS) if u]


_URL = re.compile(r"://[^\s'\"<>]+")

# ---- redaction: one rule shared with the MCP server, pinned by tests/fixtures/scrub-vectors.json ----

_SHORT = 4              # a piece shorter than this (skeleton length) is never searched inside other text
_MARK = "[redacted]"
_IGNORABLE = ((0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180D), (0x180F, 0x180F),
              (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0xE0000, 0xE0FFF))


def _gap(c: str) -> bool:
    """White space, control, format, surrogate or default-ignorable: skipped when text is compared."""
    return c.isspace() or unicodedata.category(c) in ("Cc", "Cf", "Cs") or any(a <= ord(c) <= b for a, b in _IGNORABLE)


def _skel(s: str) -> tuple:
    """(skeleton, where): s without _gap characters, lower-cased where the lower case is one character;
    where[i] is the index in s of skeleton[i]."""
    out, where = [], []
    for i, c in enumerate(s):
        if _gap(c):
            continue
        low = c.lower()
        out.append(low if len(low) == 1 else c)
        where.append(i)
    return "".join(out), where


def _json(s: str) -> list:
    """s JSON-escaped without its quotes, as json.dumps writes it with non-ASCII as written and as \\uXXXX, each
    with '/' as written and escaped (PHP json_encode); and s with only its '/' escaped (the MCP server's F3)."""
    forms = []
    for ascii_only in (False, True):
        j = json.dumps(s, ensure_ascii=ascii_only)[1:-1]
        forms += [j, j.replace("/", "\\/")]
    return forms + [s.replace("/", "\\/")]


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


_FORMS = (   # every written form of a piece the node may quote; a new form is a new entry
    lambda a: [a],                                                                         # F0 as configured
    lambda a: [urllib.parse.unquote(a)],                                                   # F1 percent-decoded
    lambda a: [urllib.parse.quote(urllib.parse.unquote(a), safe="!'()*~-._")],             # F2 percent-encoded
    lambda a: [j for x in (a, urllib.parse.unquote(a)) for j in _json(x)],                # F3 JSON-escaped (\uXXXX or not), '/' plain or escaped; '/' escaped alone
    lambda a: [_html(x) for x in (a, urllib.parse.unquote(a))],                            # F4 HTML-escaped
)


def _split(url: str) -> dict:
    """The parts of a URL as urllib.parse.urlsplit cuts them, without its clean-ups (the MCP server cuts the same)."""
    rest, i = url, url.find(":")
    if i > 0 and url[0].isascii() and url[0].isalpha() and all(c.isascii() and (c.isalnum() or c in "+-.") for c in url[:i]):
        rest = url[i + 1:]
    netloc = ""
    if rest[:2] == "//":
        j = min([k for k in (rest.find(d, 2) for d in "/?#") if k >= 0] or [len(rest)])
        netloc, rest = rest[2:j], rest[j:]
    path, _, frag = rest.partition("#")
    path, _, query = path.partition("?")
    userinfo, at, hostport = netloc.rpartition("@")
    user, _, pw = userinfo.partition(":")
    host = hostport[1:].partition("]")[0] if hostport.startswith("[") else hostport.partition(":")[0]
    return {"netloc": netloc, "path": path, "query": query, "frag": frag, "user": user, "pw": pw, "at": bool(at),
            "host": host.lower(), "bare": re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url)}


def _longs(pieces) -> set:
    return {k for a in pieces if a for form in _FORMS for f in form(a) for k in [_skel(f)[0]] if len(k) >= _SHORT}


def _cut(text: str, longs: list) -> str:
    """text with every match of a skeleton in longs replaced, from its first to its last character."""
    sk, where = _skel(text)
    for f in longs:
        hits, i = [], sk.find(f)
        while i >= 0:
            hits.append((where[i], where[i + len(f) - 1] + 1))
            i = sk.find(f, i + len(f))
        if hits:
            out, last = [], 0
            for a, b in hits:
                out += [text[last:a], _MARK]
                last = b
            text = "".join(out) + text[last:]
            sk, where = _skel(text)
    return text


def _alone(text: str, k: str) -> str:
    """text with every match of the skeleton k replaced where it stands alone: no [A-Za-z0-9] in text just
    before its first or just after its last character."""
    sk, where = _skel(text)
    out, last, i = [], 0, sk.find(k)
    while i >= 0:
        a, b = where[i], where[i + len(k) - 1] + 1
        if not any(0 <= j < len(text) and text[j].isascii() and text[j].isalnum() for j in (a - 1, b)):
            out += [text[last:a], _MARK]
            last, i = b, sk.find(k, i + len(k))
        else:
            i = sk.find(k, i + 1)
    return "".join(out) + text[last:]


def _order(keys) -> list:
    return sorted(keys, key=lambda k: (-len(k), k))


def _redact(text: str, secrets: list) -> str | None:
    """Text with every piece of every URL in secrets taken out, in every written form (_FORMS, and base64
    of the user info), compared on the skeleton (_skel). Pieces: the URL, without scheme, netloc, host, path,
    query, each query value, user, password, fragment, each path segment, each host label that is not
    only ASCII letters (a word such as rpc or invalid stays; a label under _SHORT is never searched).
    A path segment under _SHORT is removed only where it stands alone (not inside a longer word). None
    when a query value, user, password or fragment is under _SHORT: it cannot be removed safely, so the
    caller drops the text."""
    longs, shorts = set(), set()
    for url in secrets:
        p = _split(url)
        creds = []
        for kv in p["query"].split("&"):
            if "=" in kv:
                v = kv.split("=", 1)[1]
                creds += [v, urllib.parse.unquote(v.replace("+", " "))]
        creds += [p["user"], p["pw"], p["frag"]]
        creds = [c for c in creds + [urllib.parse.unquote(c) for c in creds] if c]
        if any(len(_skel(c)[0]) < _SHORT for c in creds):
            return None
        segs = [s for s in p["path"].split("/") if s]
        segs += [urllib.parse.unquote(s) for s in segs]
        labels = [h for h in p["host"].split(".") if not (h.isascii() and h.isalpha())]   # a key as a sub-domain
        longs |= _longs([url, p["bare"], p["netloc"], p["host"], p["path"], p["query"], *creds, *segs, *labels])
        if p["at"]:   # an Authorization: Basic header the node may print back
            basic = base64.b64encode(f"{urllib.parse.unquote(p['user'])}:{urllib.parse.unquote(p['pw'])}".encode()).decode()
            longs |= {_skel(basic)[0]}                                                      # F5, as sent
        shorts |= {s for s in segs if 0 < len(_skel(s)[0]) < _SHORT}
    text = _cut(text, _order(longs))
    for p in sorted(shorts, key=lambda s: (-len(_skel(s)[0]), _skel(s)[0], s)):
        text = _alone(text, _skel(p)[0])
    return text


def _scrub(text: str, secret: str) -> str | None:
    """Node text with the RPC URL taken out (_redact), then any URL left keeps only its scheme.
    None: the caller drops the node text altogether."""
    text = _redact(text, [secret])
    return None if text is None else _URL.sub("://[redacted]", text)


def seal(obj, secrets=None):
    """obj (dict, list, str) with every configured RPC URL taken out of every string: the URL whole, without
    its scheme, and host with path (and query), in the forms F0-F4. Never a lone piece, so a chain name that
    is also a path segment stays; never withholds. The last check on every answer the tools give."""
    longs = set()
    for url in _secrets() if secrets is None else secrets:
        p = _split(url)
        if p["host"]:
            longs |= _longs([url, p["bare"]])
            for np in (p["netloc"] + p["path"], p["netloc"].rpartition("@")[2] + p["path"]):   # with and without user info
                longs |= _longs([np, *([f"{np}?{p['query']}"] if p["query"] else [])])
    if not longs:
        return obj
    longs = _order(longs)

    def walk(o):
        if isinstance(o, str):
            return _cut(o, longs)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return [walk(v) for v in o] if isinstance(o, (list, tuple)) else o
    return walk(obj)


def rpc(url: str, method: str, params: list):
    r = _http(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, secret=url)
    if not isinstance(r, dict) or "result" not in r:
        err = r.get("error") if isinstance(r, dict) else None
        msg = err.get("message", "no result") if isinstance(err, dict) else "no result"
        clean = _scrub(msg, url) if isinstance(msg, str) else None
        raise BPError("rpc_error", f"your RPC answered an error: {clean[:200]}" if clean is not None
                      else "your RPC answered an error (its text is withheld: it could carry your RPC key)")
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
    if dec.get("tx") and not pv["canExecute"]:
        # Your node measured this route as not executable: its answer outranks the hosted decode.
        out["txError"] = {"code": "not_executable",
                          "message": "your node says this route cannot execute now: no transaction is returned"}
    elif dec.get("tx"):
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


# ---- USD value ---------------------------------------------------------------------------------
# What amount_in of a token is worth in the chain's dollar token, measured only with the pinned
# Quoter (previewPlan) through Multicall3 on YOUR node, every via in one eth_call at one block.
# The dollar token (USDC; USDG on Robinhood Chain) is treated as exactly 1 USD.

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL_PLAN = "49d5f197"       # previewPlan(address,address,uint256)
SEL_AGG3 = "82ad56cb"       # aggregate3((address,bool,bytes)[])
SEL_BLOCK = "42cbb15c"      # getBlockNumber()
SEL_DECIMALS = "313ce567"   # decimals()
# On Arbitrum and Robinhood Chain (Orbit) block.number, so Multicall3.getBlockNumber, is the L1 block;
# ArbSys(0x64).arbBlockNumber() is the block the RPC tags count (measured 11-20). Read the block there.
ARBSYS = "0x0000000000000000000000000000000000000064"
SEL_ARB_BLOCK = "a3b1b31d"  # arbBlockNumber()
ARBSYS_BLOCK_CHAINS = (42161, 4663)
SEL_ERROR = "08c379a0"      # Error(string)
# Revert data named the way worker/usd.ts names it (viem decodeErrorResult over BLAZE_ERRORS_ABI): selector -> (name, types)
USD_ERRORS = {SEL_ERROR: ("Error", ["string"]), "4e487b71": ("Panic", ["uint256"]),
              "cfcad14c": ("GateQuoteCall", ["uint256"]), "c85d7a0e": ("InputProbe", ["bool"]),
              "3940bcb0": ("QuoteDeltas", ["int256", "int256"]), "765afc20": ("QuoterE", ["uint16"]),
              "3d0ccdde": ("SolverE", ["uint16"]), "9a3fb915": ("HubE", ["uint16"]), "62b0a386": ("RouterE", ["uint16"])}
AGG3_IN = [(["address", "bool", "bytes"],)]
AGG3_OUT = [(["bool", "bytes"],)]
MAX_VIAS = 8                # direct included; every via has at most 2 hops of tokens (T -> X -> dollar)
MAX_USD_AMOUNT = (1 << 128) - 1
SNAPSHOT_TRIES = 2          # a block hash lost to a reorg restarts route discovery once on a new block, then not measured
NOT_MEASURED = "not measured"
USD_BASIS = ("Each via is the netOut the Quoter reports for A in one block: after the 0.28% fee, including price impact; "
             "an X -> dollar leg is priced at refX and scaled linearly. The value is the minimum over measured vias; "
             "the dollar token counts as 1 USD.")   # worker/usd.ts USD_BASIS, verbatim


def dollar_token(cid: int) -> tuple:
    t = CHAINS[cid]["tokens"]
    sym = "USDC" if "USDC" in t else "USDG"
    return sym, t[sym].lower()


def format_units(v: int, decimals: int) -> str:
    """viem formatUnits: integer part, then the fraction without trailing zeros."""
    s = str(abs(v)).rjust(decimals, "0")
    i, f = s[:len(s) - decimals], s[len(s) - decimals:].rstrip("0")
    return ("-" if v < 0 else "") + (i or "0") + ("." + f if f else "")


def _call3(target: str, sel: str, types=(), args=()) -> list:
    return [target, True, "0x" + sel + _enc(list(types), list(args)).hex()]


def _plan_call(cid: int, a: str, b: str, amt: int) -> list:
    return _call3(CHAINS[cid]["quoter"], SEL_PLAN, ["address", "address", "uint256"], [a, b, amt])


def aggregate3_data(calls: list) -> str:
    return "0x" + SEL_AGG3 + _enc(AGG3_IN, [calls]).hex()


class _SnapshotGone(Exception):
    """The node no longer serves the pinned block hash (a reorg), or takes no EIP-1898 blockHash at all."""


def _snapshot(url: str, tag: str) -> tuple:
    """The block both aggregates read, from YOUR node: (number, EIP-1898 pin by hash). A pin by number would read
    the block that replaced it after a reorg at the same height, and the height check would still pass."""
    h = rpc(url, "eth_getBlockByNumber", [tag, False])
    num, bh = (h.get("number"), h.get("hash")) if isinstance(h, dict) else (None, None)
    if not (isinstance(num, str) and num.startswith("0x") and isinstance(bh, str) and len(bh) == 66
            and bh.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in bh[2:])):
        raise BPError("rpc_error", "your RPC returned no block header to pin the measurement to")
    return int(num, 16), {"blockHash": bh.lower(), "requireCanonical": True}


def _multicall(url: str, calls: list, tag) -> list:
    try:
        raw = rpc(url, "eth_call", [{"to": MULTICALL3, "data": aggregate3_data(calls)}, tag])
    except BPError:
        if isinstance(tag, dict):   # the hash is gone or not understood: never fall back to a number
            raise _SnapshotGone() from None
        raise
    try:
        (res,) = abi_decode(AGG3_OUT, _hex(raw))
    except BPError as e:
        raise BPError("rpc_error", f"the Multicall3 answer from your RPC does not decode ({e})") from None
    if len(res) != len(calls):
        raise BPError("rpc_error", "the Multicall3 answer has the wrong number of results")
    return res


def _revert_reason(ret: str) -> str:
    """'call reverted (no reason)', 'call reverted: Name(args)' or 'call reverted (0xselector)', as worker/usd.ts."""
    b = _hex(ret)
    if not b:
        return "call reverted (no reason)"
    name, types = USD_ERRORS.get(b[:4].hex(), (None, []))
    if name:
        try:
            args = abi_decode(["bytes" if t == "string" else t for t in types], b[4:], strict=False)
            words = [bytes.fromhex(a[2:]).decode("utf-8", "replace") if t == "string"   # whole: cut after the scrub
                     else str(a).lower() if t == "bool" else str(a) for t, a in zip(types, args)]
            return f"call reverted: {name}({', '.join(words)})"
        except BPError:
            pass
    return f"call reverted (0x{b[:4].hex()})"


REVERT_TEXT = 200   # characters of a revert string an answer keeps, counted after the scrub
_ERROR_HEAD = "call reverted: Error("


def _capped(reason: str) -> str:
    """A scrubbed revert reason with its string cut to REVERT_TEXT characters; the whole reason cut to the
    same length when the scrub took a word of the frame."""
    if reason.startswith(_ERROR_HEAD) and reason.endswith(")"):
        return f"{_ERROR_HEAD}{reason[len(_ERROR_HEAD):-1][:REVERT_TEXT]})"
    return reason[:len(_ERROR_HEAD) + REVERT_TEXT + 1]


def _preview(r, tin: str, tout: str) -> tuple:
    """(preview, None) for a usable previewPlan answer of the leg tin -> tout, else (None, reason). Absent is not zero."""
    ok, ret = r
    if not ok:
        return None, _revert_reason(ret)
    try:
        pv = dict(zip(PV_FIELDS, abi_decode(PREVIEW_OUT, _hex(ret), strict=False)[0]))
    except BPError:
        return None, "undecodable Quoter result"
    if not pv["route"][0]:
        return None, "empty route"
    if not route_consistent(pv["route"], tin, tout):
        return None, "route does not connect the leg tokens"
    if pv["netOut"] == 0:
        return None, "netOut is 0"
    return pv, None


def _uint_result(r, types) -> int | None:
    ok, ret = r
    if not ok:
        return None
    try:
        return abi_decode(types, _hex(ret))[0]
    except BPError:
        return None


def _vias_from(results: list, specs: list) -> tuple:
    """Turn the Multicall3 results of each via's legs into via objects; also return the direct preview."""
    vias, k, direct = [], 0, None
    for path, legs in specs:
        pvs = []
        for a, b, _ in legs:
            pvs.append(_preview(results[k], a, b))
            k += 1
        via = {"path": path, "legs": [{"tokenIn": a, "tokenOut": b, "amountIn": str(amt),
                                       **({"out": str(pv["netOut"])} if pv else {})}
                                      for (a, b, amt), (pv, _) in zip(legs, pvs)]}
        if len(legs) == 1:
            direct = pvs[0][0]
        bad = [why for pv, why in pvs if pv is None]
        if len(legs) > 1:   # a two-leg via names its refX, measured or not
            via["scaledFrom"] = str(legs[1][2])
        if bad:
            via.update(status=NOT_MEASURED, reason=bad[0])   # the first leg that failed
        else:
            if len(legs) == 1:
                usd = pvs[0][0]["netOut"]
            else:
                ref = legs[1][2]
                usd = pvs[0][0]["netOut"] * pvs[1][0]["netOut"] // ref
            if usd == 0:
                via.update(status=NOT_MEASURED, reason="scaled value is 0")
            else:
                via.update(status="measured", usd=str(usd))
        vias.append(via)
    return vias, direct


def _route_tokens(pv, skip: set) -> list:
    """Tokens the direct route touches, in route order, each with the amount of it the route moves."""
    out = []
    for hop in pv["route"][0]:
        for t in (hop[0], hop[1]):
            if t not in skip:
                skip.add(t)
                ref = next((h[2] for h in pv["route"][0] if h[0] == t), 0)
                if 0 < ref <= MAX_USD_AMOUNT:   # a refX above the Router cap is no via
                    out.append((t, ref))
    return out


def _block_call(cid: int) -> list:
    return _call3(ARBSYS, SEL_ARB_BLOCK) if cid in ARBSYS_BLOCK_CHAINS else _call3(MULTICALL3, SEL_BLOCK)


def _block_of(r, pin) -> int:
    n = _uint_result(r, ["uint256"])
    if n is None or (pin is not None and n != pin):
        raise BPError("rpc_error", "your RPC did not answer at the requested block")
    return n


def _measure(cid: int, tok: str, dol: str, weth: str, amt: int, n: int, at: dict, call):
    """Every aggregate of one measurement at the block hash `at` (number n). The dollar token: its decimals;
    else (token decimals, dollar decimals, vias). Raises _SnapshotGone when the node stops serving that hash."""
    if tok == dol:   # the dollar token itself: 1 USD per unit; block and decimals still read on-chain
        res = call([_block_call(cid), _call3(dol, SEL_DECIMALS)], at)
        _block_of(res[0], n)
        return _uint_result(res[1], ["uint8"])
    specs = [([tok, dol], [(tok, dol, amt)])]
    if tok != weth:
        specs.append(([tok, weth, dol], [(tok, weth, amt), (weth, dol, 10**18)]))

    def calls(sp):
        return [_plan_call(cid, a, b, x) for _, legs in sp for a, b, x in legs]

    head = [_block_call(cid), _call3(tok, SEL_DECIMALS), _call3(dol, SEL_DECIMALS)]
    res = call(head + calls(specs), at)
    _block_of(res[0], n)
    dt, dd = _uint_result(res[1], ["uint8"]), _uint_result(res[2], ["uint8"])
    vias, direct = _vias_from(res[3:], specs)
    found = _route_tokens(direct, {tok, dol, weth})[:MAX_VIAS - len(specs)] if direct else []
    if found:   # every via again in ONE eth_call pinned at the same block HASH
        specs += [([tok, x, dol], [(tok, x, amt), (x, dol, ref)]) for x, ref in found]
        res2 = call([_block_call(cid), *calls(specs)], at)
        if _uint_result(res2[0], ["uint256"]) != n:
            raise BPError("rpc_error", "your RPC did not honour the block pin of the second request")
        vias, _ = _vias_from(res2[1:], specs)
    return dt, dd, vias


def usd_value(chain, token: str, amount_in, block=None) -> dict:
    cid = resolve_chain(chain)
    weth = CHAINS[cid]["tokens"]["WETH"].lower()
    tok = weth if str(token).strip().upper() == "ETH" else resolve_token(cid, token)
    amt = _uint(amount_in, "amount_in")
    if amt > MAX_USD_AMOUNT:
        raise BPError("bad_request", "amount_in: must be at most 2^128 - 1 (the Router cap)")
    pin = _uint(block, "block") if block not in (None, "") else None
    sym, dol = dollar_token(cid)
    out = {"chainId": cid, "version": 1, "label": f"USD (via {sym})", "dollarToken": dol,
           "dollarDecimals": None, "token": tok, "amountIn": str(amt), "tokenDecimals": None,
           "blockNumber": None, "usdValue": None, "usd": NOT_MEASURED, "unitPrice": NOT_MEASURED,
           "vias": [], "spread": None, "rpc": {"requests": 0}, "basis": USD_BASIS}
    url = rpc_url(cid)
    if int(rpc(url, "eth_chainId", []), 16) != cid:
        raise BPError("rpc_wrong_chain", f"your RPC for {CHAINS[cid]['name']} serves another chain")
    tag = "latest" if pin is None else hex(pin)
    sent = [0]

    def call(calls, at):
        sent[0] += 1
        return _multicall(url, calls, at)

    for _ in range(SNAPSHOT_TRIES):
        n, at = _snapshot(url, tag)
        if pin is not None and n != pin:
            raise BPError("rpc_error", "your RPC did not answer at the requested block")
        try:
            got = _measure(cid, tok, dol, weth, amt, n, at, call)
        except _SnapshotGone:
            continue    # restart discovery on a new snapshot; never reuse candidates from the lost one
        break
    else:
        out["rpc"] = {"requests": sent[0]}
        return out      # no block hash held for both aggregates: not measured, never a value
    if tok == dol:
        dd = got
        out.update(blockNumber=str(n), tokenDecimals=dd, dollarDecimals=dd, usdValue=str(amt), rpc={"requests": sent[0]},
                   vias=[{"path": [dol], "status": "identity", "usd": str(amt), "legs": []}])
        if dd is not None:
            out.update(usd=format_units(amt, dd), unitPrice=format_units(amt * 10 ** (dd + 18) // (amt * 10 ** dd), 18))
        return out
    dt, dd, vias = got
    for v in vias:   # a revert text is bytes from your node: the same question as rpc() asks of its error text,
        # on the whole text: a cut first can leave the key less its last characters, which no piece matches
        if "reason" in v:
            clean = _scrub(v["reason"], url)
            v["reason"] = _capped(clean) if clean is not None else "call reverted (text withheld: it could carry your RPC key)"
    measured = [int(v["usd"]) for v in vias if v["status"] == "measured"]
    out.update(blockNumber=str(n), tokenDecimals=dt, dollarDecimals=dd, vias=vias, rpc={"requests": sent[0]})
    if measured:
        usd = min(measured)   # conservative: the least any via says the amount is worth
        out["usdValue"] = str(usd)
        if dd is not None:
            out["usd"] = format_units(usd, dd)
        if dt is not None and dd is not None:
            out["unitPrice"] = format_units(usd * 10 ** (dt + 18) // (amt * 10 ** dd), 18)
    if len(measured) >= 2:
        lo, hi = min(measured), max(measured)
        out["spread"] = {"min": str(lo), "max": str(hi), "diff": str(hi - lo), "bps": str((hi - lo) * 10_000 // hi)}
    return out
