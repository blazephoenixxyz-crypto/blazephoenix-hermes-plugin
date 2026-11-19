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
ZERO = "0x" + "00" * 20
LEG = [ZERO, ZERO, 0, 0, 0, False, False, 0, 0, "0x" + "00" * 32]


def preview(net, hops):
    route = [[[a, b, amt, 0, [LEG]] for a, b, amt in hops], 0, 0, 0, 0, 0, 0, False, False]
    pv = [route] + [net if i == 3 else 0 for i in range(10)] + [0, ZERO, True]
    return bp._enc(bp.PREVIEW_OUT, [pv, [[], 0, 0, 0, 0, 0, 0, False, False], False])


def error(msg):
    return bytes.fromhex(bp.SEL_ERROR) + eth_abi.encode(["string"], [msg])


class Node:
    """Fake RPC: plans[(a, b, amount)] = (netOut, hops) | bytes (revert data); decimals[token] = int."""

    def __init__(self, plans, decimals=None, honour_pin=True):
        self.plans, self.honour_pin, self.tags, self.calls = plans, honour_pin, [], []
        self.decimals = {T: 18, D: 6, W: 18, **(decimals or {})}
        self.head = BLOCK

    def answer(self, target, cd, block):
        sel, args = cd[:4].hex(), cd[4:]
        if sel == bp.SEL_BLOCK and target == bp.MULTICALL3.lower():
            return True, eth_abi.encode(["uint256"], [block])
        if sel == bp.SEL_DECIMALS:
            d = self.decimals.get(target)
            return (False, b"") if d is None else (True, eth_abi.encode(["uint8"], [d]))
        if sel == bp.SEL_PLAN and target == bp.CHAINS[8453]["quoter"].lower():
            a, b, amt = eth_abi.decode(["address", "address", "uint256"], args)
            p = self.plans.get((a.lower(), b.lower(), amt), b"")
            return (False, p) if isinstance(p, bytes) else (True, preview(*p))
        return False, b""

    def __call__(self, url, method, params):
        if method == "eth_chainId":
            return "0x2105"
        assert method == "eth_call" and params[0]["to"] == bp.MULTICALL3
        data = bytes.fromhex(params[0]["data"][2:])
        assert data[:4].hex() == bp.SEL_AGG3
        (calls,) = eth_abi.decode(["(address,bool,bytes)[]"], data[4:])
        tag = params[1]
        self.tags.append(tag)
        if tag == "latest":
            self.head += 1          # the chain moves between two requests at "latest"
            block = self.head
        else:
            block = int(tag, 16) if self.honour_pin else self.head + 7
        out = []
        for target, allow, cd in calls:
            ok, ret = self.answer(target.lower(), cd, block)
            if not ok and not allow:  # Multicall3 reverts the whole batch
                raise bp.BPError("rpc_error", "your RPC answered an error: Multicall3: call failed")
            out.append((ok, ret))
        self.calls.append(len(calls))
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

def test_parity_vector(node):  # mutants: M26 (max instead of min), M27 (scale by outD), M33 (format_units)
    n = node(parity_plans())
    r = usd()
    assert (r["usdValue"], r["usd"], r["unitPrice"]) == ("3000000", "3", "1.5")
    assert r["spread"] == {"min": "3000000", "max": "5000000", "diff": "2000000", "bps": 4000}
    assert [(v["path"], v["status"], v["usd"]) for v in r["vias"]] == [
        ([T, D], "measured", "5000000"), ([T, W, D], "measured", "3000000")]
    assert r["vias"][1]["scaledFrom"] == str(E18)
    assert r["vias"][1]["legs"] == [{"tokenIn": T, "tokenOut": W, "amountIn": str(2 * E18), "out": str(10**15)},
                                    {"tokenIn": W, "tokenOut": D, "amountIn": str(E18), "out": "3000000000"}]
    assert (r["label"], r["dollarToken"], r["tokenDecimals"], r["dollarDecimals"]) == ("USD (via USDC)", D, 18, 6)
    assert r["rpc"] == {"requests": 1} and n.calls == [6] and n.tags == ["latest"]
    assert r["blockNumber"] == str(BLOCK + 1)
    json.dumps(r)   # JSON-safe: every amount is a decimal string


def test_vias_that_agree_have_zero_spread(node):  # mutant: M28 (spread bps divides by min)
    node({(T, D, 2 * E18): (3_000_000, [(T, D, 2 * E18)]), (T, W, 2 * E18): (10**15, [(T, W, 2 * E18)]),
          (W, D, E18): (3_000_000_000, [(W, D, E18)])})
    assert usd()["spread"] == {"min": "3000000", "max": "3000000", "diff": "0", "bps": 0}


@pytest.mark.parametrize("lo,hi,bps", [(1, 3, 6666), (9_999, 10_000, 1), (1, 10**30, 9999)])
def test_spread_bps_rounds_down(node, lo, hi, bps):  # mutant: M28 (bps over min)
    node({(T, D, 2 * E18): (hi, [(T, D, 2 * E18)]), (T, W, 2 * E18): (E18, [(T, W, 2 * E18)]),
          (W, D, E18): (lo, [(W, D, E18)])})
    r = usd()
    assert r["spread"]["bps"] == bps and r["usdValue"] == str(lo)


# ---- absent is not zero ---------------------------------------------------------------------------

def test_one_via_reverts_the_others_still_count(node):  # mutant: M29 (allowFailure false)
    p = parity_plans()
    p[(W, D, E18)] = error("no route")
    r = usd() if node(p) else None
    assert r["vias"][1]["status"] == "not measured" and r["vias"][1]["reason"] == "leg 2: reverted: no route"
    assert r["vias"][1]["legs"][0]["out"] == str(10**15) and "out" not in r["vias"][1]["legs"][1]
    assert "usd" not in r["vias"][1]
    assert r["usdValue"] == "5000000" and r["spread"] is None


def test_all_vias_fail_is_not_measured_never_zero(node):  # mutant: M30 (netOut 0 counted as a value)
    node({(T, D, 2 * E18): (0, [(T, D, 2 * E18)]), (T, W, 2 * E18): b"\x12\x34\x56\x78"})
    r = usd()
    assert r["usdValue"] is None and r["usd"] == "not measured" and r["unitPrice"] == "not measured"
    assert [v["reason"] for v in r["vias"]] == ["netOut is 0", "leg 1: reverted (selector 0x12345678)"]
    assert r["spread"] is None and "0" not in (r["usd"], r["unitPrice"])


def test_empty_route_is_not_measured(node):  # mutant: M31 (0-hop route accepted)
    p = parity_plans()
    p[(T, D, 2 * E18)] = (5_000_000, [])
    r = usd() if node(p) else None
    assert r["vias"][0] == {"path": [T, D], "status": "not measured", "reason": "route has 0 hops",
                            "legs": [{"tokenIn": T, "tokenOut": D, "amountIn": str(2 * E18)}]}
    assert r["usdValue"] == "3000000" and r["spread"] is None


def test_value_below_one_unit_is_not_measured(node):  # mutant: M32 (a scaled 0 counted as a value)
    p = parity_plans()
    p[(T, W, 2 * E18)] = (1, [(T, W, 2 * E18)])
    p[(W, D, E18)] = (E18 - 1, [(W, D, E18)])          # 1 * (1e18 - 1) // 1e18 == 0
    r = usd() if node(p) else None
    assert r["vias"][1]["status"] == "not measured" and r["usdValue"] == "5000000"


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
    n = node({}, decimals={g: 6})
    n_call = n.__call__
    monkeypatch.setattr(bp, "rpc", lambda u, m, p: "0x1237" if m == "eth_chainId" else n_call(u, m, p))
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
    assert e.value.code == "rpc_error" and n.tags == ["0x7b"]
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
    assert r["rpc"] == {"requests": 2} and n.tags == ["latest", hex(BLOCK + 1)] and n.calls == [6, 6]
    v = r["vias"][2]
    assert (v["path"], v["status"], v["usd"], v["scaledFrom"]) == ([T, X[0], D], "measured", "4000000", str(ref))
    assert r["usdValue"] == "3000000" and r["blockNumber"] == str(BLOCK + 1)


def test_second_request_must_honour_the_pin(node):  # mutant: M41 (pin of request 2 not checked)
    node(routed([X[0]], 10**6), honour_pin=False)
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
    assert bp._revert_reason("0x" + bp.SEL_ERROR + bad.hex()) == "reverted (selector 0x08c379a0)"


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
