"""Host stub: bp.py imports `agent.secret_scope.get_secret` from the Hermes host at call time.

Outside a Hermes install (CI, a clean venv) that module does not exist, so a minimal stand-in is
registered here. It mirrors only what bp.py uses, with the behaviour of the host's get_secret
(hermes-agent 6b21d05, agent/secret_scope.py) in a single-profile process: no scope installed and
multiplexing inactive, so the value is read from os.environ and a miss returns `default`.
When the real host package is importable it is used untouched."""
import os
import sys
import types

try:
    import agent.secret_scope  # noqa: F401  (real host present)
except ImportError:
    def get_secret(name, default=None):
        val = os.environ.get(name)
        return val if val is not None else default

    _agent = types.ModuleType("agent")
    _agent.__path__ = []
    _scope = types.ModuleType("agent.secret_scope")
    _scope.get_secret = get_secret
    _agent.secret_scope = _scope
    sys.modules["agent"] = _agent
    sys.modules["agent.secret_scope"] = _scope
