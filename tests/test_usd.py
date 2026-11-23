"""Offline tests of the USD value: a fake node answers Multicall3.aggregate3 with eth-abi (an independent
encoder), so the plugin's own codec is checked on the way out and on the way back. No network.

Each test names the mutant that turns it red (see tests/mutants.py, which runs them)."""

import json
import pathlib
import random
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bp  # noqa: E402

eth_abi = pytest.importorskip("eth_abi")

D = bp.CHAINS[8453]["tokens"]["USDC"].lower()
W = bp.CHAINS[8453]["tokens"]["WETH"].lower()
T = "0x" + "aa" * 20
X = ["0x%040x" % (0xb0 + i) for i in range(12)]
E18 = 10**18
BLOCK = 30_000_000
L1_BLOCK = 26_452_919   # what block.number answers on Arbitrum chains
ARBSYS = "0x" + "00" * 19 + "64"
ZERO = "0x" + "00" * 20
LEG = [ZERO, ZERO, 0, 0, 0, False, False, 0, 0, "0x" + "00" * 32]


def preview(net, hops):
    """hops: (tokenIn, tokenOut, amountIn[, number of legs, default 1])."""
    route = [[[h[0], h[1], h[2], 0, [LEG] * (h[3] if len(h) > 3 else 1)] for h in hops], 0, 0, 0, 0, 0, 0, False, False]
    pv = [route] + [net if i == 3 else 0 for i in range(10)] + [0, ZERO, True]
    return bp._enc(bp.PREVIEW_OUT, [pv, [[], 0, 0, 0, 0, 0, 0, False, False], False])


def error(msg):
    return bytes.fromhex(bp.SEL_ERROR) + eth_abi.encode(["string"], [msg])


class Raw(bytes):
    """A previewPlan call that succeeds with these exact return bytes."""


API_BASIS = ("Each via is the netOut the Quoter reports for A in one block: after the 0.28% fee, including price impact; "
             "an X -> dollar leg is priced at refX and scaled linearly. The value is the minimum over measured vias; "
             "the dollar token counts as 1 USD.")   # worker/usd.ts USD_BASIS, verbatim


class Node:
    """Fake RPC: plans[(a, b, amount)] = (netOut, hops) | bytes (revert data); decimals[token] = int."""

    def __init__(self, plans, decimals=None, honour_pin=True, chain=8453):
        self.plans, self.honour_pin, self.tags, self.calls, self.chain = plans, honour_pin, [], [], chain
        self.decimals = {T: 18, D: 6, W: 18, **(decimals or {})}
        self.head = BLOCK
        self.fork, self.reorg_after, self.reorg_plans, self.blockhash, self.header_skew = 0, None, None, True, 0
        self.canon = {}             # block number -> fork whose block is canonical at that height

    def hash_of(self, block, fork):
        return "0x" + f"{fork:02x}{block:062x}"

    def reorg(self):
        """Replace the canonical block at every height with another one, from now on with other plans."""
        self.fork += 1
        self.plans = self.reorg_plans
        self.canon = {b: self.fork for b in self.canon}

    def answer(self, target, cd, block):
        sel, args = cd[:4].hex(), cd[4:]
        arb = self.chain in (42161, 4663)   # block.number is the L1 block there (measured 11-20)
        if sel == bp.SEL_BLOCK and target == bp.MULTICALL3.lower():
            return True, eth_abi.encode(["uint256"], [L1_BLOCK if arb else block])
        if target == ARBSYS:   # the precompile on Arbitrum chains; elsewhere no code: success, no data
            return True, eth_abi.encode(["uint256"], [block]) if arb and sel == "a3b1b31d" else b""
        if sel == bp.SEL_DECIMALS:
            d = self.decimals.get(target)
            return (False, b"") if d is None else (True, eth_abi.encode(["uint8"], [d]))
        if sel == bp.SEL_PLAN and target == bp.CHAINS[self.chain]["quoter"].lower():
            a, b, amt = eth_abi.decode(["address", "address", "uint256"], args)
            p = self.plans.get((a.lower(), b.lower(), amt), b"")
            if isinstance(p, Raw):
                return True, bytes(p)
            return (False, p) if isinstance(p, bytes) else (True, preview(*p))
        return False, b""

    def __call__(self, url, method, params):
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_getBlockByNumber":
            self.tags.append(params[0])
            if params[0] == "latest":
                self.head += 1      # the chain moves between two measurements at "latest"
            b = (self.head if params[0] == "latest" else int(params[0], 16)) + self.header_skew
            self.canon.setdefault(b, self.fork)
            return {"number": hex(b), "hash": self.hash_of(b, self.canon[b])}
        assert method == "eth_call" and params[0]["to"] == bp.MULTICALL3
        data = bytes.fromhex(params[0]["data"][2:])
        assert data[:4].hex() == bp.SEL_AGG3
        (calls,) = eth_abi.decode(["(address,bool,bytes)[]"], data[4:])
        tag = params[1]
        if isinstance(tag, dict):   # EIP-1898: a block hash, never resolved to a number by height
            if not self.blockhash:
                raise bp.BPError("rpc_error", "your RPC answered an error: invalid argument 1: hex string expected")
            assert tag["requireCanonical"] is True
            hit = [b for b, f in self.canon.items() if self.hash_of(b, f) == tag["blockHash"]]
            if not hit:             # the block was reorganised away: the node no longer serves that hash
                raise bp.BPError("rpc_error", "your RPC answered an error: header not found")
            block = hit[0] if self.honour_pin is True or (self.honour_pin == "first" and not self.calls) else self.head + 7
            self.tags.append("hash:" + hex(block))
        elif tag == "latest":
            self.tags.append(tag)
            self.head += 1          # the chain moves between two requests at "latest"
            block = self.head
        else:
            self.tags.append(tag)
            block = int(tag, 16) if self.honour_pin else self.head + 7
        out = []
        for target, allow, cd in calls:
            ok, ret = self.answer(target.lower(), cd, block)
            if not ok and not allow:  # Multicall3 reverts the whole batch
                raise bp.BPError("rpc_error", "your RPC answered an error: Multicall3: call failed")
            out.append((ok, ret))
        self.calls.append(len(calls))
        if self.reorg_after == len(self.calls):
            self.reorg()
        return "0x" + eth_abi.encode(["(bool,bytes)[]"], [out]).hex()


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setenv("BLAZEPHOENIX_RPC_BASE", "https://node.example")

    def make(plans, **kw):
        n = Node(plans, **kw)
        monkeypatch.setattr(bp, "rpc", n)
        return n
    return make


def parity_plans():
    return {(T, D, 2 * E18): (5_000_000, [(T, D, 2 * E18)]),
            (T, W, 2 * E18): (10**15, [(T, W, 2 * E18)]),
            (W, D, E18): (3_000_000_000, [(W, D, E18)])}


def usd(token=T, amount=2 * E18, **kw):
    return bp.usd_value("base", token, str(amount), **kw)


# ---- the parity vector shared with the SDK and the API ------------------------------------------

def test_parity_vector(node):  # mutants: M26, M27, M33 (format_units), M48 (bps an int), M49 (basis)
    n = node(parity_plans())
    r = usd()
    assert (r["usdValue"], r["usd"], r["unitPrice"]) == ("3000000", "3", "1.5")
    assert r["spread"] == {"min": "3000000", "max": "5000000", "diff": "2000000", "bps": "4000"}
    assert [(v["path"], v["status"], v["usd"]) for v in r["vias"]] == [
        ([T, D], "measured", "5000000"), ([T, W, D], "measured", "3000000")]
    assert r["vias"][1]["scaledFrom"] == str(E18)
    assert r["vias"][1]["legs"] == [{"tokenIn": T, "tokenOut": W, "amountIn": str(2 * E18), "out": str(10**15)},
                                    {"tokenIn": W, "tokenOut": D, "amountIn": str(E18), "out": "3000000000"}]
    assert (r["label"], r["dollarToken"], r["tokenDecimals"], r["dollarDecimals"]) == ("USD (via USDC)", D, 18, 6)
    assert r["rpc"] == {"requests": 1} and n.calls == [6] and n.tags == ["latest", "hash:" + hex(BLOCK + 1)]
    assert r["blockNumber"] == str(BLOCK + 1) and r["basis"] == API_BASIS
    json.dumps(r)   # JSON-safe: every amount is a decimal string


def test_vias_that_agree_have_zero_spread(node):  # mutant: M28 (spread bps divides by min)
    node({(T, D, 2 * E18): (3_000_000, [(T, D, 2 * E18)]), (T, W, 2 * E18): (10**15, [(T, W, 2 * E18)]),
          (W, D, E18): (3_000_000_000, [(W, D, E18)])})
    assert usd()["spread"] == {"min": "3000000", "max": "3000000", "diff": "0", "bps": "0"}


@pytest.mark.parametrize("lo,hi,bps", [(1, 3, 6666), (9_999, 10_000, 1), (1, 10**30, 9999)])
def test_spread_bps_rounds_down(node, lo, hi, bps):  # mutant: M28 (bps over min)
    node({(T, D, 2 * E18): (hi, [(T, D, 2 * E18)]), (T, W, 2 * E18): (E18, [(T, W, 2 * E18)]),
          (W, D, E18): (lo, [(W, D, E18)])})
    r = usd()
    assert r["spread"]["bps"] == str(bps) and r["usdValue"] == str(lo)


# ---- absent is not zero ---------------------------------------------------------------------------

def test_one_via_reverts_the_others_still_count(node):  # mutants: M29 (allowFailure false), M50 (scaledFrom only when measured)
    p = parity_plans()
    p[(W, D, E18)] = error("no route")
    r = usd() if node(p) else None
    assert r["vias"][1]["status"] == "not measured" and r["vias"][1]["reason"] == "call reverted: Error(no route)"
    assert r["vias"][1]["legs"][0]["out"] == str(10**15) and "out" not in r["vias"][1]["legs"][1]
    assert "usd" not in r["vias"][1] and r["vias"][1]["scaledFrom"] == str(E18)
    assert r["usdValue"] == "5000000" and r["spread"] is None


def test_all_vias_fail_is_not_measured_never_zero(node):  # mutants: M30 (netOut 0 counted), M51 (leg prefix)
    node({(T, D, 2 * E18): (0, [(T, D, 2 * E18)]), (T, W, 2 * E18): b"\x12\x34\x56\x78"})
    r = usd()
    assert r["usdValue"] is None and r["usd"] == "not measured" and r["unitPrice"] == "not measured"
    assert [v["reason"] for v in r["vias"]] == ["netOut is 0", "call reverted (0x12345678)"]
    assert r["spread"] is None and "0" not in (r["usd"], r["unitPrice"])


def test_empty_route_is_not_measured(node):  # mutant: M31 (0-hop route accepted)
    p = parity_plans()
    p[(T, D, 2 * E18)] = (5_000_000, [])
    r = usd() if node(p) else None
    assert r["vias"][0] == {"path": [T, D], "status": "not measured", "reason": "empty route",
                            "legs": [{"tokenIn": T, "tokenOut": D, "amountIn": str(2 * E18)}]}
    assert r["usdValue"] == "3000000" and r["spread"] is None


def test_value_below_one_unit_is_not_measured(node):  # mutants: M32 (a scaled 0 counted as a value), M50
    p = parity_plans()
    p[(T, W, 2 * E18)] = (1, [(T, W, 2 * E18)])
    p[(W, D, E18)] = (E18 - 1, [(W, D, E18)])          # 1 * (1e18 - 1) // 1e18 == 0
    r = usd() if node(p) else None
    assert r["vias"][1]["status"] == "not measured" and r["usdValue"] == "5000000"
    assert (r["vias"][1]["reason"], r["vias"][1]["scaledFrom"]) == ("scaled value is 0", str(E18))
    assert "usd" not in r["vias"][1]


def test_decimals_failure_leaves_unit_price_not_measured(node):  # mutant: M34 (unit price without dT)
    node(parity_plans(), decimals={T: None})
    r = usd()
    assert r["tokenDecimals"] is None and r["unitPrice"] == "not measured"
    assert (r["usdValue"], r["usd"]) == ("3000000", "3")


def test_dollar_decimals_failure_leaves_usd_not_measured(node):  # mutant: M35 (usd formatted without dD)
    node(parity_plans(), decimals={D: None, T: 300 % 256})
    r = usd()
    assert r["dollarDecimals"] is None and r["usd"] == "not measured" and r["unitPrice"] == "not measured"
    assert r["usdValue"] == "3000000"


# ---- reasons and route shape, as worker/usd.ts words them ------------------------------------------

def custom(sig, types, args):
    keccak = pytest.importorskip("Crypto.Hash.keccak")
    return bytes.fromhex(keccak.new(digest_bits=256, data=sig.encode()).hexdigest()[:8]) + eth_abi.encode(types, args)


@pytest.mark.parametrize("data,reason", [
    (b"", "call reverted (no reason)"),
    ("Error", "call reverted: Error(no route)"),
    (("Panic(uint256)", ["uint256"], [17]), "call reverted: Panic(17)"),
    (("QuoterE(uint16)", ["uint16"], [3]), "call reverted: QuoterE(3)"),
    (("SolverE(uint16)", ["uint16"], [65535]), "call reverted: SolverE(65535)"),
    (("HubE(uint16)", ["uint16"], [0]), "call reverted: HubE(0)"),
    (("RouterE(uint16)", ["uint16"], [7]), "call reverted: RouterE(7)"),
    (("GateQuoteCall(uint256)", ["uint256"], [2**256 - 1]), "call reverted: GateQuoteCall(%d)" % (2**256 - 1)),
    (("InputProbe(bool)", ["bool"], [True]), "call reverted: InputProbe(true)"),
    (("InputProbe(bool)", ["bool"], [False]), "call reverted: InputProbe(false)"),
    (("QuoteDeltas(int256,int256)", ["int256", "int256"], [-5, 2**255 - 1]), "call reverted: QuoteDeltas(-5, %d)" % (2**255 - 1)),
    (b"\x12\x34\x56\x78", "call reverted (0x12345678)"),
    (b"\x12\x34\x56\x78" + bytes(32), "call reverted (0x12345678)"),
    (b"\x12", "call reverted (0x12)"),
])
def test_revert_reasons_match_the_api(node, data, reason):  # mutants: M52 (empty data), M53 (error table), M54 (bool words)
    if data == "Error":
        data = error("no route")
    elif isinstance(data, tuple):
        data = custom(*data)
    p = parity_plans()
    p[(T, D, 2 * E18)] = data
    r = usd() if node(p) else None
    assert r["vias"][0]["reason"] == reason and r["usdValue"] == "3000000"


@pytest.mark.parametrize("sig", ["Panic(uint256)", "QuoterE(uint16)", "InputProbe(bool)", "QuoteDeltas(int256,int256)"])
def test_custom_error_with_a_short_payload_is_named_by_selector(node, sig):  # mutant: M60 (short payload raises)
    data = custom(sig, [], [])
    p = parity_plans()
    p[(T, D, 2 * E18)] = data
    r = usd() if node(p) else None
    assert r["vias"][0]["reason"] == "call reverted (0x%s)" % data[:4].hex()


def test_first_failing_leg_gives_the_reason(node):  # mutant: M55 (last failing leg)
    p = parity_plans()
    p[(T, W, 2 * E18)] = error("first")
    p[(W, D, E18)] = error("second")
    r = usd() if node(p) else None
    assert r["vias"][1]["reason"] == "call reverted: Error(first)" and "out" not in r["vias"][1]["legs"][0]


KEY_URL = "https://node.invalid/v3/KEY0123456789abcdef"


@pytest.mark.parametrize("leg", [(T, D, 2 * E18), (W, D, E18)])          # the 1-leg and the 2-leg via
@pytest.mark.parametrize("text", ["bad key KEY0123456789abcdef", "see node.invalid/v3/KEY0123456789abcdef",
                                  "key KEY0123​456789abcdef"])
def test_usd_revert_text_never_echoes_the_rpc_key(node, monkeypatch, leg, text):  # mutant: M83
    """The revert text of a via is bytes from your node: it gets the same scrub as the node's error text."""
    p = parity_plans()
    p[leg] = error(text)
    node(p)
    monkeypatch.setenv("BLAZEPHOENIX_RPC_BASE", KEY_URL)
    r = usd()
    reason = [v["reason"] for v in r["vias"] if v["status"] != "measured"]
    assert len(reason) == 1 and reason[0].startswith("call reverted: Error(") and "[redacted]" in reason[0]
    assert "456789abcdef" not in reason[0].lower()


def test_usd_revert_text_withheld_when_a_credential_is_short(node, monkeypatch):  # mutant: M83
    p = parity_plans()
    p[(W, D, E18)] = error("bad k a1b")
    node(p)
    monkeypatch.setenv("BLAZEPHOENIX_RPC_BASE", "https://node.invalid/rpc?k=a1b")
    assert usd()["vias"][1]["reason"] == "call reverted (text withheld: it could carry your RPC key)"


@pytest.mark.parametrize("raw", [b"", b"\x00" * 31, b"\xff" * 64])
def test_undecodable_quoter_result_is_not_measured(node, raw):  # mutant: M56 (decode failure wording)
    p = parity_plans()
    p[(W, D, E18)] = Raw(raw)
    r = usd() if node(p) else None
    assert (r["vias"][1]["status"], r["vias"][1]["reason"]) == ("not measured", "undecodable Quoter result")
    assert r["usdValue"] == "5000000"


@pytest.mark.parametrize("key,hops", [
    ((T, D, 2 * E18), [(T, W, 2 * E18)]),                              # direct: ends elsewhere
    ((T, D, 2 * E18), [(W, D, 2 * E18)]),                              # direct: starts elsewhere
    ((T, D, 2 * E18), [(T, X[0], 2 * E18), (X[1], D, 10**6)]),         # direct: hops do not chain
    ((T, D, 2 * E18), [(T, D, 2 * E18, 0)]),                           # direct: a hop with no venue leg
    ((T, W, 2 * E18), [(T, D, 2 * E18)]),                              # leg 1 of the WETH via
    ((W, D, E18), [(T, D, E18)]),                                      # leg 2 of the WETH via
])
def test_route_that_does_not_connect_the_leg_is_not_measured(node, key, hops):  # mutant: M57 (route shape per leg)
    p = parity_plans()
    p[key] = (9 * 10**6, hops)
    n = node(p)
    r = usd()
    bad = 0 if key[1] == D and key[0] == T else 1
    assert (r["vias"][bad]["status"], r["vias"][bad]["reason"]) == ("not measured", "route does not connect the leg tokens")
    assert r["usdValue"] == ("3000000" if bad == 0 else "5000000") and r["rpc"]["requests"] == 1 and len(n.calls) == 1


def test_disconnected_direct_route_discovers_nothing(node):  # mutant: M57
    p = routed([X[0]], 10**6)
    p[(T, D, 2 * E18)] = (5_000_000, [(T, X[0], 2 * E18), (X[1], D, 10**6)])
    r = usd() if node(p) else None
    assert r["rpc"]["requests"] == 1 and len(r["vias"]) == 2 and r["usdValue"] == "3000000"


# ---- inputs and anchors ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount", ["0", str(2**128), str(2**256 - 1), "-1", "1e18"])
def test_amount_out_of_range_is_refused(node, amount):  # mutant: M36 (uint128 cap off by one)
    node({})
    with pytest.raises(bp.BPError) as e:
        usd(amount=amount)
    assert e.value.code == "bad_request"


@pytest.mark.parametrize("amount", [1, 10_000_000, 2**128 - 1])
def test_dollar_token_is_identity_measured_in_one_call(node, amount):  # mutant: M37 (identity branch dropped)
    n = node({})
    r = usd("USDC", amount)
    assert (r["usdValue"], r["vias"], r["rpc"]) == (
        str(amount), [{"path": [D], "status": "identity", "usd": str(amount), "legs": []}], {"requests": 1})
    assert (r["unitPrice"], r["tokenDecimals"], r["dollarDecimals"]) == ("1", 6, 6)
    assert r["usd"] == bp.format_units(amount, 6) and n.calls == [2] and r["blockNumber"] == str(BLOCK + 1)


def test_dollar_token_without_decimals_is_not_measured(node):  # mutant: M47 (identity usd without dD)
    node({}, decimals={D: None})
    r = usd("USDC", 10_000_000)
    assert (r["usd"], r["unitPrice"], r["usdValue"]) == ("not measured", "not measured", "10000000")


def test_robinhood_dollar_is_usdg(node, monkeypatch):
    monkeypatch.setenv("BLAZEPHOENIX_RPC_ROBINHOOD", "https://node.example")
    g = bp.CHAINS[4663]["tokens"]["USDG"].lower()
    node({}, decimals={g: 6}, chain=4663)
    r = bp.usd_value("robinhood", "USDG", "2500000")
    assert (r["label"], r["usd"], r["dollarToken"]) == ("USD (via USDG)", "2.5", g)


@pytest.mark.parametrize("token", ["WETH", "ETH", W.upper().replace("0X", "0x")])
def test_weth_has_no_weth_via(node, token):  # mutant: M38 (WETH via for WETH)
    n = node({(W, D, E18): (3_000_000_000, [(W, D, E18)])})
    r = usd(token, E18)
    assert [v["path"] for v in r["vias"]] == [[W, D]] and n.calls == [4]
    assert (r["usd"], r["unitPrice"], r["spread"]) == ("3000", "3000", None)


def test_user_block_is_pinned_and_checked(node):  # mutant: M39 (pinned block not checked)
    n = node(parity_plans(), honour_pin=False)
    with pytest.raises(bp.BPError) as e:
        usd(block=123)
    assert e.value.code == "rpc_error" and n.tags == ["0x7b", "hash:" + hex(BLOCK + 7)]
    n = node(parity_plans())
    assert usd(block=123)["blockNumber"] == "123"


# ---- tokens the route touches ----------------------------------------------------------------------

def routed(hops_x, ref):
    """Direct route T -> X... -> D through the given intermediates; each X moves `ref` of itself."""
    path = [T] + hops_x + [D]
    hops = [(path[i], path[i + 1], 2 * E18 if i == 0 else ref) for i in range(len(path) - 1)]
    p = parity_plans()
    p[(T, D, 2 * E18)] = (5_000_000, hops)
    for i, x in enumerate(dict.fromkeys(hops_x)):
        if x not in (W, D, T):
            p[(T, x, 2 * E18)] = (7 * 10**6, [(T, x, 2 * E18)])
            p[(x, D, ref)] = (4_000_000 + i, [(x, D, ref)])
    return p


def test_route_token_adds_a_via_in_one_pinned_request(node):  # mutant: M40 (second request at latest)
    ref = 7 * 10**6
    n = node(routed([X[0]], ref))
    r = usd()
    assert r["rpc"] == {"requests": 2} and n.calls == [6, 6]
    assert n.tags == ["latest", "hash:" + hex(BLOCK + 1), "hash:" + hex(BLOCK + 1)]   # one header, one hash
    v = r["vias"][2]
    assert (v["path"], v["status"], v["usd"], v["scaledFrom"]) == ([T, X[0], D], "measured", "4000000", str(ref))
    assert r["usdValue"] == "3000000" and r["blockNumber"] == str(BLOCK + 1)


def test_arbitrum_block_is_the_l2_block_rpc_tags_count(node, monkeypatch):  # mutant: M61 (block from Multicall3 everywhere)
    monkeypatch.setenv("BLAZEPHOENIX_RPC_ARBITRUM", "https://node.example")
    d, w = (bp.CHAINS[42161]["tokens"][k].lower() for k in ("USDC", "WETH"))
    x, ref = X[0], 7 * 10**6
    n = node({(T, d, 2 * E18): (5_000_000, [(T, x, 2 * E18), (x, d, ref)]), (T, w, 2 * E18): (10**15, [(T, w, 2 * E18)]),
              (w, d, E18): (3_000_000_000, [(w, d, E18)]), (T, x, 2 * E18): (7 * 10**6, [(T, x, 2 * E18)]),
              (x, d, ref): (4_000_000, [(x, d, ref)])}, decimals={d: 6}, chain=42161)
    r = bp.usd_value("arbitrum", T, str(2 * E18))
    assert r["rpc"] == {"requests": 2} and n.tags == ["latest", "hash:" + hex(BLOCK + 1), "hash:" + hex(BLOCK + 1)]
    assert r["blockNumber"] == str(BLOCK + 1) and r["usdValue"] == "3000000"


def test_robinhood_block_at_a_given_block(node, monkeypatch):  # mutant: M62 (4663 left out)
    monkeypatch.setenv("BLAZEPHOENIX_RPC_ROBINHOOD", "https://node.example")
    g = bp.CHAINS[4663]["tokens"]["USDG"].lower()
    n = node({(T, g, 2 * E18): (2_000_000, [(T, g, 2 * E18)])}, decimals={g: 6}, chain=4663)
    r = bp.usd_value("robinhood", T, str(2 * E18), block=123)
    assert n.tags == ["0x7b", "hash:0x7b"] and r["blockNumber"] == "123" and r["usdValue"] == "2000000"


def test_block_call_without_data_fails_closed(node, monkeypatch):  # mutant: M63 (ArbSys on every chain)
    n = node(parity_plans())
    n.chain = 8453
    monkeypatch.setattr(bp, "ARBSYS_BLOCK_CHAINS", (8453,))   # 0x64 has no code on Base: no block number
    with pytest.raises(bp.BPError) as e:
        usd()
    assert e.value.code == "rpc_error"


def test_second_request_must_honour_the_pin(node):  # mutant: M41 (pin of request 2 not checked)
    node(routed([X[0]], 10**6), honour_pin="first")
    with pytest.raises(bp.BPError) as e:
        usd()
    assert e.value.code == "rpc_error" and "block pin" in str(e.value)


def test_route_tokens_are_deduplicated_and_skip_anchors(node):  # mutant: M42 (duplicates kept)
    n = node(routed([X[0], W, X[1], X[0], D, T, X[1]], 10**6))
    r = usd()
    assert [v["path"][1] for v in r["vias"]][1:] == [W, X[0], X[1]] and n.calls == [6, 1 + 1 + 2 + 2 * 2]


def test_at_most_eight_vias(node):  # mutant: M43 (cap of 9)
    n = node(routed(X[:10], 10**6))
    r = usd()
    assert len(r["vias"]) == 8 and [v["path"][1] for v in r["vias"][2:]] == X[:6]
    assert n.calls == [6, 1 + 1 + 2 + 2 * 6] and r["rpc"]["requests"] == 2


@pytest.mark.parametrize("ref,found", [(2**128 - 1, True), (2**128, False), (2**256 - 1, False)])
def test_route_amount_above_the_router_cap_adds_no_via(node, ref, found):  # mutants: M58 (cap dropped), M59 (cap off by one)
    n = node(routed([X[0]], ref))
    r = usd()
    assert [v["path"] for v in r["vias"]][2:] == ([[T, X[0], D]] if found else [])
    assert r["rpc"]["requests"] == (2 if found else 1) and len(n.calls) == (2 if found else 1)


def test_failed_direct_quote_discovers_nothing(node):
    p = routed([X[0]], 10**6)
    p[(T, D, 2 * E18)] = error("paused")
    r = usd() if node(p) else None
    assert r["rpc"]["requests"] == 1 and len(r["vias"]) == 2 and r["usdValue"] == "3000000"


# ---- codec and constants ---------------------------------------------------------------------------

def test_usd_selectors_match_their_signatures():  # mutant: M44 (previewPlan selector)
    keccak = pytest.importorskip("Crypto.Hash.keccak")
    for sel, sig in [(bp.SEL_PLAN, "previewPlan(address,address,uint256)"),
                     (bp.SEL_AGG3, "aggregate3((address,bool,bytes)[])"),
                     (bp.SEL_BLOCK, "getBlockNumber()"), (bp.SEL_DECIMALS, "decimals()"),
                     (bp.SEL_ERROR, "Error(string)")]:
        assert keccak.new(digest_bits=256, data=sig.encode()).hexdigest()[:8] == sel, sig
    for sel, (name, types) in bp.USD_ERRORS.items():   # mutant: M53 (error table selector)
        sig = f"{name}({','.join(types)})"
        assert keccak.new(digest_bits=256, data=sig.encode()).hexdigest()[:8] == sel, sig


@pytest.mark.parametrize("seed", range(100))
def test_bytes_codec_matches_eth_abi(seed):  # mutants: M45 (bytes padding), M46 (bytes length unchecked)
    r = random.Random(seed)
    calls = [["0x%040x" % r.getrandbits(160), r.random() < 0.5, "0x" + r.randbytes(r.choice([0, 1, 31, 32, 33, 68, 100])).hex()]
             for _ in range(r.choice([0, 1, 2, 5]))]
    ours = bp._enc(bp.AGG3_IN, [calls])
    assert ours == eth_abi.encode(["(address,bool,bytes)[]"], [[(a, b, bytes.fromhex(c[2:])) for a, b, c in calls]])
    assert bp.abi_decode(bp.AGG3_IN, ours) == [calls]
    if calls:
        last = bytes.fromhex(calls[-1][2][2:])
        bad = ours[:-32 * ((len(last) + 31) // 32) - 32] + (len(last) + 32 + 1).to_bytes(32, "big") + ours[len(ours) - 32 * ((len(last) + 31) // 32):]
        with pytest.raises(bp.BPError):
            bp.abi_decode(bp.AGG3_IN, bad)


@pytest.mark.parametrize("v,d,s", [(0, 6, "0"), (1, 6, "0.000001"), (1_500_000, 6, "1.5"), (10**18, 18, "1"),
                                   (123, 0, "123"), (-15, 1, "-1.5"), (1_000_000_100, 6, "1000.0001")])
def test_format_units_matches_viem(v, d, s):  # mutant: M33 (trailing zeros kept)
    assert bp.format_units(v, d) == s


@pytest.mark.parametrize("extra", [1, 32, 2**255])
def test_lying_bytes_length_is_refused_even_when_not_strict(extra):  # mutant: M46 (bytes length unchecked)
    good = eth_abi.encode(["bytes"], [b"abc"])
    assert bp.abi_decode(["bytes"], good, strict=False) == ["0x616263"]
    bad = good[:32] + (3 + 29 + extra).to_bytes(32, "big") + good[64:]
    with pytest.raises(bp.BPError):
        bp.abi_decode(["bytes"], bad, strict=False)
    assert bp._revert_reason("0x" + bp.SEL_ERROR + bad.hex()) == "call reverted (0x08c379a0)"


def test_bad_multicall_answers_fail_closed(monkeypatch):
    monkeypatch.setenv("BLAZEPHOENIX_RPC_BASE", "https://node.example")
    for raw in ["0x", "0x" + eth_abi.encode(["(bool,bytes)[]"], [[(True, b"")]]).hex()]:
        monkeypatch.setattr(bp, "rpc", lambda u, m, p, raw=raw: "0x2105" if m == "eth_chainId" else raw)
        with pytest.raises(bp.BPError) as e:
            usd()
        assert e.value.code == "rpc_error"


def test_tool_handler_and_manifest(monkeypatch):
    monkeypatch.delenv("BLAZEPHOENIX_RPC_BASE", raising=False)
    import importlib.util
    spec = importlib.util.spec_from_file_location("bpplugin_usd", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bpplugin_usd"] = mod
    spec.loader.exec_module(mod)
    out = json.loads(mod.usd_value({"token": "USDC", "amount_in": "10000000"}))
    assert out == {"ok": False, "code": "rpc_required", "error": out["error"]}   # fails closed without a node
    bad = json.loads(mod.usd_value({"token": "USDC", "amount_in": "0"}))
    assert bad == {"ok": False, "code": "bad_request", "error": "amount_in: must be positive"}
    assert "blazephoenix_usd_value" in (ROOT / "plugin.yaml").read_text()
    names = []
    mod.register(type("C", (), {"register_tool": lambda self, **k: names.append(k["name"])})())
    assert "blazephoenix_usd_value" in names


# ---- reorg at the same height: both aggregates pin one block HASH (EIP-1898) -----------------------

def reorg_plans():
    """After the reorg the direct route crosses Y instead of X, and the Y via is the lowest."""
    ref, y = 7 * 10**6, X[1]
    p = parity_plans()
    p[(T, D, 2 * E18)] = (5_000_000, [(T, y, 2 * E18), (y, D, ref)])
    p[(T, y, 2 * E18)] = (7 * 10**6, [(T, y, 2 * E18)])
    p[(y, D, ref)] = (1_000_000, [(y, D, ref)])
    return p


def test_reorg_between_the_two_aggregates_never_mixes_blocks(node):  # mutant: M64 (second aggregate pinned by number)
    n = node(routed([X[0]], 7 * 10**6))
    n.reorg_after, n.reorg_plans = 1, reorg_plans()   # the block of the first aggregate is replaced at the same height
    r = usd()
    paths = [v["path"] for v in r["vias"]]
    assert [T, X[0], D] not in paths and [T, X[1], D] in paths    # every via from the block that replaced it
    assert r["usdValue"] == "1000000" and r["blockNumber"] == str(BLOCK + 2)
    assert n.tags == ["latest", "hash:" + hex(BLOCK + 1),   # its hash is lost on the 2nd aggregate: discovery restarts
                      "latest", "hash:" + hex(BLOCK + 2), "hash:" + hex(BLOCK + 2)]
    assert r["rpc"] == {"requests": 4}     # every eth_call sent, the lost one included


def test_reorg_on_every_attempt_is_not_measured(node):  # mutant: M65 (snapshot retried forever or valued)
    n = node(routed([X[0]], 7 * 10**6))
    n.reorg_after, n.reorg_plans = 1, routed([X[0]], 7 * 10**6)
    orig = n.reorg

    def again():
        orig()
        n.reorg_after = len(n.calls) + 1   # and again after the next aggregate
    n.reorg = again
    r = usd()
    assert (r["usd"], r["usdValue"], r["vias"], r["blockNumber"]) == (bp.NOT_MEASURED, None, [], None)
    assert r["unitPrice"] == bp.NOT_MEASURED and n.tags.count("latest") == 2 and r["rpc"] == {"requests": 4}


@pytest.mark.parametrize("token", [T, D])
def test_node_without_blockhash_is_not_measured(node, token):  # mutant: M66 (fallback to a number tag)
    n = node(parity_plans())
    n.blockhash = False
    r = usd(token=token)
    assert (r["usd"], r["usdValue"], r["vias"], r["unitPrice"]) == (bp.NOT_MEASURED, None, [], bp.NOT_MEASURED)
    assert all(t == "latest" or t.startswith("hash:") for t in n.tags)


def test_pinned_block_number_is_resolved_to_its_hash(node):  # mutant: M67 (header read at latest)
    n = node(parity_plans())
    r = usd(block=123)
    assert n.tags == ["0x7b", "hash:0x7b"] and r["blockNumber"] == "123"


def test_header_of_another_block_than_the_pin_fails_closed(node):  # mutant: M68 (header number not checked)
    n = node(parity_plans())
    n.header_skew = 1
    with pytest.raises(bp.BPError) as e:
        usd(block=123)
    assert e.value.code == "rpc_error" and "requested block" in str(e.value)
