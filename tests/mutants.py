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
