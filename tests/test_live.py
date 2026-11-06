"""Live tests on Base: the eth_call runs on the node in BLAZEPHOENIX_RPC_BASE (skipped when unset).

They also play a lying server: prepare or decode answers are altered in flight and must be refused."""

import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bp  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("BLAZEPHOENIX_RPC_BASE"), reason="BLAZEPHOENIX_RPC_BASE not set")
RCP = "0x000000000000000000000000000000000000dEaD"
ONE_WETH = str(10**18)


def test_quote_and_verified_calldata_on_base():
    q = bp.quote("base", "WETH", "USDC", ONE_WETH, recipient=RCP)
    assert int(q["amountOut"]) > 0 and q["canExecute"] is True
    assert q["phoenixCheck"]["agree"] is True
    v = q["verification"]
    assert v["ok"] and "route equals your node's preview" in v["checked"]
    assert q["unsignedTx"]["to"].lower() == bp.CHAINS[8453]["router"].lower()
    assert int(v["userMinOut"]) >= int(q["amountOut"]) - int(q["amountOut"]) * 50 // 10000
    # mutant that must fail: one byte of the verified calldata lowered (userMinOut - 1)
    d = q["unsignedTx"]["data"]
    k = 2 + 8 + 2 * 64
    bad = d[:k] + "%064x" % (int(v["userMinOut"]) - 1) + d[k + 64:]
    with pytest.raises(bp.BPError) as e:
        bp.verify_swap(8453, {**q["unsignedTx"], "data": bad}, token_in=q["tokenIn"], token_out=q["tokenOut"],
                       amount_in=10**18, recipient=RCP, min_out=int(v["userMinOut"]))
    assert e.value.code == "calldata_mismatch"


def test_quote_only_small_amount():
    q = bp.quote("base", "WETH", "USDC", str(10**14))       # 0.0001 WETH, no recipient: quote only
    assert int(q["amountOut"]) > 0 and "unsignedTx" not in q


def test_dust_fails_cleanly():
    with pytest.raises(bp.BPError) as e:                    # 1000 wei: the Quoter reverts, reported as such
        bp.quote("base", "WETH", "USDC", "1000")
    assert e.value.code == "rpc_error"


def _tamper(monkeypatch, path, edit):
    real = bp._http

    def fake(url, body=None, secret=None):
        r = real(url, body, secret)
        return edit(r) if path in url else r
    monkeypatch.setattr(bp, "_http", fake)


@pytest.mark.parametrize("edit", [
    lambda r: {**r, "call": {**r["call"], "to": "0x" + "11" * 20}},                     # another contract
    lambda r: {**r, "request": {**r["request"], "amountIn": "1"}},                     # another amount
    lambda r: {**r, "contracts": {**r["contracts"], "router": "0x" + "22" * 20}},      # another router
])
def test_lying_prepare_is_refused(monkeypatch, edit):
    _tamper(monkeypatch, "/api/quote/prepare", edit)
    with pytest.raises(bp.BPError) as e:
        bp.quote("base", "WETH", "USDC", ONE_WETH)
    assert e.value.code == "prepare_mismatch"


def test_lying_decode_is_refused(monkeypatch):
    _tamper(monkeypatch, "/api/quote/decode", lambda r: {**r, "amountOut": str(int(r["amountOut"]) + 1)})
    with pytest.raises(bp.BPError) as e:
        bp.quote("base", "WETH", "USDC", ONE_WETH)
    assert e.value.code == "decode_mismatch"


def test_lying_calldata_is_refused(monkeypatch):
    def edit(r):
        tx = r.get("tx")
        if tx:   # recipient swapped for an attacker inside otherwise valid calldata
            d = tx["data"]
            k = 2 + 8 + 3 * 64
            r = {**r, "tx": {**tx, "data": d[:k] + "%064x" % 0xBEEF + d[k + 64:]}}
        return r
    _tamper(monkeypatch, "/api/quote/decode", edit)
    with pytest.raises(bp.BPError) as e:
        bp.quote("base", "WETH", "USDC", ONE_WETH, recipient=RCP)
    assert e.value.code == "calldata_mismatch"


def test_tool_handlers_return_json():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bpplugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bpplugin"] = mod
    spec.loader.exec_module(mod)
    out = json.loads(mod.chains({}))
    assert out["ok"] and any(c["chainId"] == 8453 and c["rpcConfigured"] for c in out["result"])
    assert os.environ["BLAZEPHOENIX_RPC_BASE"] not in json.dumps(out)
