"""Offline tests: ABI codec, selectors, Phoenix Check and calldata verification. No network.

Each test names the mutant that turns it red (see tests/mutants.py, which runs them)."""

import copy
import json
import pathlib
import random
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bp  # noqa: E402

FX = json.loads((ROOT / "tests/fixtures/base_weth_usdc.json").read_text())
REQ = FX["request"]
NOW = int(REQ["deadline"]) - 120          # the moment the fixture was prepared
TIN, TOUT = REQ["tokenIn"].lower(), REQ["tokenOut"].lower()
AMT, MIN, RCP = int(REQ["amountIn"]), int(REQ["userMinOut"]), REQ["recipient"].lower()
PV = dict(zip(bp.PV_FIELDS, bp.abi_decode(bp.PREVIEW_OUT, bytes.fromhex(FX["result"][2:]), strict=False)[0]))


def verify(tx=None, **kw):
    args = dict(token_in=TIN, token_out=TOUT, amount_in=AMT, recipient=RCP, min_out=MIN,
                deadline=int(REQ["deadline"]), route=PV["route"], approval=FX["approval"], now=NOW)
    args.update(kw)
    return bp.verify_swap(8453, tx or FX["tx"], **args)


def refused(tx=None, **kw):
    with pytest.raises(bp.BPError) as e:
        verify(tx, **kw)
    assert e.value.code == "calldata_mismatch"
    return str(e.value)


# ---- codec: differential against eth-abi (test-only dependency) -------------------------------

def _type_str(t):
    if isinstance(t, str):
        return t
    if isinstance(t, tuple):
        return _type_str(t[0]) + "[]"
    return "(" + ",".join(_type_str(x) for x in t) + ")"


def _rand(t, r):
    if isinstance(t, tuple):
        return [_rand(t[0], r) for _ in range(r.choice([0, 1, 2, 3]))]
    if isinstance(t, list):
        return [_rand(x, r) for x in t]
    if t == "address":
        return "0x%040x" % r.getrandbits(160)
    if t == "bytes32":
        return "0x%064x" % r.getrandbits(256)
    if t == "bool":
        return r.random() < 0.5
    if t == "int24":
        return r.choice([-(1 << 23), -1, 0, 1, (1 << 23) - 1, r.randint(-(1 << 23), (1 << 23) - 1)])
    bits = int(t[4:])
    return r.choice([0, 1, (1 << bits) - 1, r.getrandbits(bits)])


def _norm(v):  # eth-abi returns checksummed addresses, bytes32 as bytes, tuples
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, str):
        return v.lower()
    return v


@pytest.mark.parametrize("seed", range(200))
def test_codec_matches_eth_abi_on_random_routes(seed):  # mutant: M1 (array offset), M2 (head size)
    eth_abi = pytest.importorskip("eth_abi")
    r = random.Random(seed)
    v = _rand(bp.SWAP_IN, r)
    ts = [_type_str(x) for x in bp.SWAP_IN]
    ref = eth_abi.encode(ts, [_to_eth(x, t) for x, t in zip(v, bp.SWAP_IN)])
    assert bp._enc(bp.SWAP_IN, v) == ref
    assert bp.abi_decode(bp.SWAP_IN, ref) == v
    assert _norm(eth_abi.decode(ts, ref)) == _norm(v)


def _to_eth(v, t):
    if isinstance(t, tuple):
        return [_to_eth(x, t[0]) for x in v]
    if isinstance(t, list):
        return tuple(_to_eth(x, y) for x, y in zip(v, t))
    if t == "bytes32":
        return bytes.fromhex(v[2:])
    if t == "address":
        from eth_utils import to_checksum_address
        return to_checksum_address(v)
    return v


def test_live_fixture_decodes_like_the_hosted_decode():  # mutant: M3 (PV_FIELDS order)
    assert str(PV["netOut"]) == FX["amountOut"] and str(PV["grossOut"]) == FX["grossAmountOut"]
    assert PV["canExecute"] is True and bp.route_consistent(PV["route"], TIN, TOUT)


@pytest.mark.parametrize("bad,why", [
    ("0x" + "01" + "00" * 31, "dirty address"), ("0x" + "00" * 31 + "02", "dirty bool")])
def test_dirty_words_are_refused(bad, why):  # mutant: M4 (drop dirty-word checks)
    t = "address" if "address" in why else "bool"
    with pytest.raises(bp.BPError):
        bp._scalar(t, int(bad, 16))


def test_ranges_of_small_ints():  # mutant: M5 (drop range check)
    assert bp._scalar("uint24", (1 << 24) - 1) == (1 << 24) - 1
    with pytest.raises(bp.BPError):
        bp._scalar("uint24", 1 << 24)
    assert bp._scalar("int24", (1 << 256) - 1) == -1
    with pytest.raises(bp.BPError):
        bp._scalar("int24", 1 << 23)


def test_hostile_array_length_and_truncation_fail_fast():
    with pytest.raises(bp.BPError):
        bp.abi_decode([("uint256",)], (32).to_bytes(32, "big") + (1 << 200).to_bytes(32, "big"))
    with pytest.raises(bp.BPError):
        bp.abi_decode(bp.SWAP_IN, bytes.fromhex(FX["tx"]["data"][10:])[:-1])


def test_selectors_match_their_signatures():  # mutant: M6 (wrong selector constant)
    keccak = pytest.importorskip("Crypto.Hash.keccak")
    def sel(sig):
        return keccak.new(digest_bits=256, data=sig.encode()).hexdigest()[:8]
    route = _type_str(bp.ROUTE)
    assert sel("previewPlanWithMinOut(address,address,uint256,uint256)") == bp.SEL_PREVIEW
    assert sel(f"swapExactIn({route},uint256,uint256,address,uint256)") == bp.SEL_SWAP
    assert FX["tx"]["data"][2:10] == bp.SEL_SWAP


# ---- verification: the honest calldata passes, every tamper is refused ------------------------

def test_honest_calldata_passes():
    out = verify()
    assert out["ok"] and out["userMinOut"] == str(MIN) and "route equals your node's preview" in out["checked"]


def test_every_single_byte_tamper_is_refused():  # mutant: M7 (drop route equality), M8 (non-strict decode)
    """Each of the 2308 bytes, flipped one bit at a time, is refused, with one designed exception:
    a tamper that only RAISES userMinOut passes, because a higher minimum can only make the swap
    revert, never pay less (monotone against whoever lies)."""
    data = bytearray.fromhex(FX["tx"]["data"][2:])
    passed = []
    for i in range(len(data)):
        t = bytearray(data)
        t[i] ^= 0x01
        try:
            verify({**FX["tx"], "data": "0x" + t.hex()})
            passed.append(i)
        except bp.BPError:
            pass
    for i in passed:
        assert 4 + 64 <= i < 4 + 96, f"byte {i} tamper accepted outside userMinOut"
        t = bytearray(data)
        t[i] ^= 0x01
        assert int.from_bytes(t[68:100], "big") > MIN


def test_intent_tampers_are_refused_without_the_route():
    """Standalone verification (no route known) still refuses any change to the user's intent."""
    d = FX["tx"]["data"]
    words = {1: AMT + 1, 2: MIN - 1, 3: int("beef", 16), 4: int(REQ["deadline"]) + 1}
    for word, value in words.items():                          # amountIn, userMinOut, recipient, deadline
        k = 2 + 8 + word * 64
        bad = d[:k] + "%064x" % value + d[k + 64:]
        refused({**FX["tx"], "data": bad}, route=None)


@pytest.mark.parametrize("field,value", [
    ("to", "0x7262e7483ab6f0db7b8f90eC3a9de3B02Ab36F6A"),  # a real Router, wrong chain     M9
    ("value", "1"),                                        # ETH attached                  M10
])
def test_wrong_target_or_value_is_refused(field, value):
    refused({**FX["tx"], field: value})


@pytest.mark.parametrize("kw", [
    dict(amount_in=AMT + 1), dict(amount_in=AMT - 1),               # +-1 wei                  M11
    dict(recipient="0x000000000000000000000000000000000000beef"),   # someone else             M12
    dict(min_out=MIN + 1),                                          # minimum one wei too low  M13
    dict(deadline=int(REQ["deadline"]) + 1),                        #                          M14
    dict(now=int(REQ["deadline"])),                                 # expired                  M15
    dict(now=int(REQ["deadline"]) - bp.MAX_DEADLINE_SEC - 1),       # too far in the future    M15
    dict(token_out=TIN),                                            # route to another token   M16
    dict(approval={**FX["approval"], "amount": str(AMT * 2)}),      # unlimited-ish approval   M17
    dict(approval={**FX["approval"], "spender": RCP}),              # approval to a stranger   M17
])
def test_caller_intent_mismatch_is_refused(kw):
    refused(**kw)


def test_min_out_boundary():  # range: exactly the encoded minimum passes, one above refuses
    assert verify(min_out=MIN)["ok"]
    refused(min_out=MIN + 1)


def test_zero_min_is_never_accepted():  # mutant: M13
    route = PV["route"]
    data = "0x" + bp.SEL_SWAP + bp._enc(bp.SWAP_IN, [route, AMT, 0, RCP, int(REQ["deadline"])]).hex()
    refused({**FX["tx"], "data": data}, min_out=0)


def test_other_selector_is_refused():
    with pytest.raises(bp.BPError):
        verify({**FX["tx"], "data": "0x095ea7b3" + FX["tx"]["data"][10:]})


# ---- Phoenix Check ----------------------------------------------------------------------------

def test_phoenix_check_thresholds():  # mutant: M18 (threshold off by one)
    pv = copy.deepcopy(PV)
    for impact, verdict in [(0, "ok"), (199, "ok"), (200, "caution"), (1999, "caution"), (2000, "danger")]:
        pv["route"][4] = impact
        assert bp.phoenix_check(pv, TIN, TOUT) == verdict
    pv["canExecute"] = False
    assert bp.phoenix_check(pv, TIN, TOUT) == "blocked"
    assert bp.phoenix_check(PV, TIN, TIN) == "blocked"


# ---- inputs -----------------------------------------------------------------------------------

def test_inputs():
    assert bp.resolve_chain(None) == 8453 and bp.resolve_chain("arb") == 42161 and bp.resolve_chain(10) == 10
    with pytest.raises(bp.BPError):
        bp.resolve_chain("solana")
    assert bp.resolve_token(8453, "usdc") == TOUT
    with pytest.raises(bp.BPError) as e:
        bp.resolve_token(8453, "ETH")
    assert e.value.code == "wrap_required"
    for bad in ("0", "-1", "1.5", str(1 << 256)):
        with pytest.raises(bp.BPError):
            bp._uint(bad, "x")
    assert bp._uint(str((1 << 256) - 1), "x") == (1 << 256) - 1


def test_bzpx_coming_soon():
    """BZPX: coming soon. The symbol is refused on every chain, in any case, and no BZPX address
    is in the plugin. Red under M22 (refusal dropped) and M23 (an address put back)."""
    for cid in bp.CHAINS:
        for sym in ("BZPX", "bzpx", " Bzpx "):
            with pytest.raises(bp.BPError) as e:
                bp.resolve_token(cid, sym)
            assert e.value.code == "bad_request"
            assert "BZPX: coming soon" in str(e.value) and "pass a token address" in str(e.value)
        assert "BZPX" not in bp.CHAINS[cid]["tokens"]
    src = (ROOT / "bp.py").read_text() + (ROOT / "__init__.py").read_text()
    assert not __import__("re").search(r"BZPX\s*=\s*\"0x", src, __import__("re").I)


def test_missing_rpc_fails_closed(monkeypatch):
    monkeypatch.delenv("BLAZEPHOENIX_RPC_OPTIMISM", raising=False)
    with pytest.raises(bp.BPError) as e:
        bp.quote("optimism", "WETH", "USDC", "1000")
    assert e.value.code == "rpc_required"


def test_network_errors_never_echo_the_rpc_url(monkeypatch):
    secret = "https://node.invalid/v1/SECRET-KEY-123"
    with pytest.raises(bp.BPError) as e:
        bp.rpc(secret, "eth_chainId", [])
    assert "SECRET" not in str(e.value) and "node.invalid" not in str(e.value)


def test_non_canonical_encoding_is_refused():  # mutant: M8 (canonical check dropped)
    """Trailing bytes after a valid call decode fine in a lax decoder; they are refused here."""
    refused({**FX["tx"], "data": FX["tx"]["data"] + "00" * 32})
