"""Code mutants: each one is applied to a copy of bp.py and the suite must go red.

Run: python tests/mutants.py   (live tests join in when BLAZEPHOENIX_RPC_BASE is set)."""
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
MUTANTS = {
    "M1 array element offset": ("o + 32 + _word(d, o + 32 + 32 * i)", "o + _word(d, o + 32 + 32 * i)"),
    "M2 head size": ("return 32 if _dyn(t) or isinstance(t, str) else", "return 32 if _dyn(t) or True else"),
    "M3 preview field order": ('"netOut", "ironFloor"', '"ironFloor", "netOut"'),
    "M4 dirty address accepted": ("if w >> 160:", "if False:"),
    "M5 int range unchecked": ("if w >> bits:", "if False:"),
    "M6 swap selector": ('SEL_SWAP = "36457c51"', 'SEL_SWAP = "36457c52"'),
    "M7 route equality dropped": ("if route is not None and r != route:", "if False:"),
    "M8 canonical check dropped": ("if strict and _enc(types, v) != data:", "if False:"),
    "M9 router not checked": ("if str(tx.get(\"to\", \"\")).lower() != c[\"router\"].lower():", "if False:"),
    "M10 value not checked": ("if int(str(tx.get(\"value\", \"0\")), 0) != 0:", "if False:"),
    "M11 amount not checked": ("if amt != amount_in:", "if False:"),
    "M12 recipient not checked": ("if to != rcp:", "if False:"),
    "M13 minimum not checked": ("if mn < max(min_out, 1):", "if False:"),
    "M14 deadline not pinned": ("if deadline is not None and dl != deadline:", "if False:"),
    "M15 deadline window dropped": ("if not now < dl <= now + MAX_DEADLINE_SEC:", "if False:"),
    "M16 route shape dropped": ("if not route_consistent(r, tin, tout):", "if False:"),
    "M17 approval not checked": ("if approval is not None and (", "if False and ("),
    "M18 caution threshold": ("impact >= CAUTION_IMPACT_BPS", "impact > CAUTION_IMPACT_BPS"),
    "M19 prepare not checked": ('raise BPError("prepare_mismatch"', 'pass  # BPError("prepare_mismatch"'),
    "M20 decode not cross-checked": ('raise BPError("decode_mismatch"', 'pass  # BPError("decode_mismatch"'),
    "M22 BZPX refusal dropped": ('    if s.upper() == "BZPX":\n        raise', '    if False:\n        raise'),
    "M23 BZPX address put back": ('USDC="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")),',
                                  'USDC="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", BZPX="0x' + "00" * 19 + '01")),'),
    "M24 node canExecute gate dropped": ('if dec.get("tx") and not pv["canExecute"]:', 'if False:'),
    "M25 node gate withholds every tx": ('if dec.get("tx") and not pv["canExecute"]:', 'if dec.get("tx"):'),
    "M26 USD max instead of min": ("usd = min(measured)", "usd = max(measured)"),
    "M27 USD leg 2 not scaled by refX": ('* pvs[1][0]["netOut"] // ref', '* pvs[1][0]["netOut"] // 10**18'),
    "M28 USD spread bps over min": ("(hi - lo) * 10_000 // hi", "(hi - lo) * 10_000 // lo"),
    "M29 USD allowFailure false": ('return [target, True, "0x" + sel', 'return [target, False, "0x" + sel'),
    "M30 USD netOut 0 counted": ('if pv["netOut"] == 0:', "if False:"),
    "M31 USD 0-hop route accepted": ('if not pv["route"][0]:', "if False:"),
    "M32 USD scaled 0 counted": ("if usd == 0:", "if False:"),
    "M33 USD trailing zeros kept": ('s[len(s) - decimals:].rstrip("0")', "s[len(s) - decimals:]"),
    "M34 USD unit price without token decimals": ("if dt is not None and dd is not None:", "if dd is not None and True:"),
    "M35 USD formatted without dollar decimals": ('        if dd is not None:\n            out["usd"]', '        if True:\n            out["usd"]'),
    "M36 USD uint128 cap off by one": ("if amt > MAX_USD_AMOUNT:", "if amt > MAX_USD_AMOUNT + 1:"),
    "M37 USD identity branch dropped": ("if tok == dol:   # the dollar token itself", "if False:   # the dollar token itself"),
    "M38 USD WETH via for WETH": ("if tok != weth:", "if True:"),
    "M39 USD requested block not checked": ("if n is None or (pin is not None and n != pin):", "if n is None:"),
    "M40 USD second request at latest": ("calls(specs), hex(n))", 'calls(specs), "latest")'),
    "M41 USD pin of request 2 not checked": ('if _uint_result(res2[0], ["uint256"]) != n:', "if False:"),
    "M42 USD route tokens not deduplicated": ("skip.add(t)", "pass"),
    "M43 USD cap of 9 vias": ("MAX_VIAS = 8", "MAX_VIAS = 9"),
    "M44 USD previewPlan selector": ('SEL_PLAN = "49d5f197"', 'SEL_PLAN = "49d5f198"'),
    "M45 USD bytes padding": ("bytes(-len(b) % 32)", "bytes(len(b) % 32)"),
    "M46 USD bytes length unchecked": ("if n > len(d) - o - 32:", "if False:"),
    "M47 USD identity formatted without decimals": ('        if dd is not None:\n            out.update(usd=', '        if True:\n            out.update(usd='),
    "M48 USD spread bps an int": ('"bps": str((hi - lo) * 10_000 // hi)', '"bps": (hi - lo) * 10_000 // hi'),
    "M49 USD basis says maximum": ("The value is the minimum over measured vias", "The value is the maximum over measured vias"),
    "M50 USD scaledFrom only when measured": ("if len(legs) > 1:   # a two-leg via", "if len(legs) > 1 and not bad:   # a two-leg via"),
    "M51 USD leg number in the reason": ("reason=bad[0])", 'reason="leg 1: " + bad[0])'),
    "M52 USD empty revert data named": ('    if not b:\n        return "call reverted (no reason)"', '    if False:\n        return "call reverted (no reason)"'),
    "M53 USD error table selector": ('"765afc20": ("QuoterE"', '"765afc21": ("QuoterE"'),
    "M54 USD bool argument as Python word": ('str(a).lower() if t == "bool"', 'str(a) if t == "bool"'),
    "M55 USD last failing leg named": ("reason=bad[0])", "reason=bad[-1])"),
    "M56 USD undecodable answer worded apart": ('return None, "undecodable Quoter result"', 'return None, "answer does not decode"'),
    "M57 USD leg route shape not checked": ('if not route_consistent(pv["route"], tin, tout):', "if False:"),
    "M58 USD refX above the Router cap kept": ("if 0 < ref <= MAX_USD_AMOUNT:", "if ref:"),
    "M59 USD refX cap off by one": ("if 0 < ref <= MAX_USD_AMOUNT:", "if 0 < ref < MAX_USD_AMOUNT:"),
    "M60 USD short error payload raises": ('        except BPError:\n            pass\n    return f"call reverted', '        except ZeroDivisionError:\n            pass\n    return f"call reverted'),
    "M21 URL echoed in errors": ("{'your RPC' if secret else urllib.parse.urlsplit(url).netloc}", "{url}"),
}

# Known survivors: these two also survive on main. Kept here so CI fails only on a NEW survivor.
# Remove an entry once a test kills it (the run reports it below when that happens).
KNOWN_ALIVE = {"M19 prepare not checked", "M20 decode not cross-checked"}

# Baseline: the unmutated suite must be green, otherwise every mutant would look "killed".
base = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", str(ROOT / "tests")], cwd=ROOT,
                      capture_output=True, text=True)
if base.returncode != 0:
    print(base.stdout[-2000:] + base.stderr[-2000:])
    sys.exit("baseline suite is red: mutants not meaningful")

src = (ROOT / "bp.py").read_text()
alive = []
for name, (old, new) in MUTANTS.items():
    assert src.count(old) == 1, f"{name}: anchor not found exactly once"
    with tempfile.TemporaryDirectory() as d:
        dst = pathlib.Path(d) / "plugin"
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__"))
        (dst / "bp.py").write_text(src.replace(old, new))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", str(dst / "tests")], cwd=dst,
                           capture_output=True, text=True)
        red = r.returncode != 0
        print(f"{'KILLED' if red else 'ALIVE '}  {name}")
        if not red:
            alive.append(name)
        elif name in KNOWN_ALIVE:
            print(f"NOTE  {name} is now killed: drop it from KNOWN_ALIVE")
new = [n for n in alive if n not in KNOWN_ALIVE]
if new:
    print("NEW SURVIVORS:", *new, sep="\n  ")
sys.exit(1 if new else 0)
