#!/usr/bin/env python3
"""Unit test for hop-server URL joining — run: python3 tools/test-hop-server.py

Clients reach the shim through hopBaseUrl ".../v1", so every relayed path
already starts with "/v1/". An upstream that carries its own version suffix
must not get a second one: "https://api.x.ai/v1" + "/v1/chat/completions"
used to produce a 404 while the dashboard probe (which normalizes /v1 itself)
still reported the endpoint healthy.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

CASES = [
    ("https://api.x.ai/v1", "/v1/chat/completions",
     "https://api.x.ai/v1/chat/completions"),
    ("https://open.bigmodel.cn/api/coding/paas/v4", "/v1/chat/completions",
     "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"),
    ("https://api.deepseek.com", "/v1/chat/completions",
     "https://api.deepseek.com/v1/chat/completions"),
    ("http://127.0.0.1:8642", "/v1/models",
     "http://127.0.0.1:8642/v1/models"),
    # Non-versioned paths are never rewritten, even on a versioned upstream.
    ("https://api.x.ai/v1", "/healthz", "https://api.x.ai/v1/healthz"),
]


def load(upstream: str):
    os.environ["HERMES_HOP_UPSTREAM"] = upstream
    spec = importlib.util.spec_from_file_location(
        "hop_server_under_test", HERE / "hop-server.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load hop-server.py next to this test")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    failed = 0
    for upstream, path, expected in CASES:
        got = load(upstream).upstream_url(path, upstream)
        if got == expected:
            print("PASS %s + %s" % (upstream, path))
        else:
            failed += 1
            print("FAIL %s + %s -> %s (want %s)" % (upstream, path, got, expected))

    # Hot-read: bindings file (dashboard-written) must win over the
    # launch-time env, and its absence must fall back to that env — this is
    # what makes channel swaps restart-free.
    tmpdir = Path(tempfile.mkdtemp())
    bf = tmpdir / "model-bindings.json"
    bf.write_text(json.dumps({"agents": {"*": {"upstream": "https://hot.example/v1"}}}))
    os.environ["OPENGROK_BINDINGS"] = str(bf)
    mod = load("http://env-fallback:1")
    got = mod.current_upstream()
    if got == "https://hot.example/v1":
        print("PASS hot-read: bindings upstream beats env")
    else:
        failed += 1
        print("FAIL hot-read -> %s (want https://hot.example/v1)" % got)
    bf.unlink()
    got = mod.current_upstream()
    if got == "http://env-fallback:1":
        print("PASS hot-read: missing bindings falls back to env")
    else:
        failed += 1
        print("FAIL hot-read fallback -> %s (want http://env-fallback:1)" % got)

    print("hop-server: %s" % ("ok" if not failed else "%d fail" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
