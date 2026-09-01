#!/usr/bin/env python3
"""remote-dashboard — Tailscale-friendly Web Control Panel for Grok Bot / OpenGrok.

Lets you remotely configure models, inspect live routing logs, test endpoints,
re-wrap Grok Bot host after updates, and restart sand-host over Tailscale.

A background watchdog re-injects the opengrok wrap when the sandbox
supervisor regenerates host-main.cjs from a pristine copy (which drops the
injection). Set OPENGROK_WRAP_WATCHDOG_SEC=0 to disable; default 30s.

Run ON the Grok Bot box:
    python3 tools/remote-dashboard.py --port 8888 --host 0.0.0.0

Then open from your phone or Mac on the same Tailnet:
    http://<grok-bot-tailscale-ip>:8888
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_DATA = Path("/home/box/sand-data")
DEFAULT_HOST = Path("/home/box/sand-host/host-main.cjs")
SESSION_LOG = Path("/tmp/opengrok-session.log")

try:
    import wrap_proto_session
except Exception:
    wrap_proto_session = None

def _watchdog_interval() -> int:
    raw = os.environ.get("OPENGROK_WRAP_WATCHDOG_SEC", "30") or "30"
    try:
        return max(0, int(raw))
    except ValueError:
        return 30


WATCHDOG_SEC = _watchdog_interval()
# Files the injected host require()s at runtime — kept in sync by the watchdog.
RUNTIME_FILES = (
    "opengrok-runtime.cjs",
    "openai-hop-session.cjs",
    "provider-maps.cjs",
    "provider-maps-hop.cjs",
    "hop-server.py",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_hop_server():
    """Ensure hop-server.py is running if bindings are configured."""
    data_dir = DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT
    bindings = get_bindings(data_dir)
    if not bindings:
        return
    agent = (bindings.get("agents") or {}).get("*") or (
        list((bindings.get("agents") or {}).values())[0] if bindings.get("agents") else None
    )
    if not agent or not agent.get("upstream"):
        return

    upstream = agent.get("upstream")
    key_file = data_dir / ".api_key"
    api_key = os.environ.get("API_SERVER_KEY", "")
    if not api_key and key_file.is_file():
        try:
            api_key = key_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    try:
        r = subprocess.run(["pgrep", "-f", "hop-server.py"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return
    except Exception:
        pass

    try:
        env = os.environ.copy()
        env["HERMES_HOP_UPSTREAM"] = upstream
        env["HERMES_HOP_PORT"] = "18790"
        env["HERMES_HOP_HOST"] = "127.0.0.1"
        if api_key:
            env["API_SERVER_KEY"] = api_key
        subprocess.Popen([sys.executable, str(HERE / "hop-server.py")], env=env, start_new_session=True)
    except Exception:
        pass


def get_tailscale_ip() -> str:
    """Try finding tailscale IP."""
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_sand_host_status() -> dict:
    """Check sand-host process status."""
    try:
        r = subprocess.run(["pgrep", "-f", "host-main.cjs"], capture_output=True, text=True)
        pids = [int(p) for p in r.stdout.strip().split() if p.isdigit()]
        return {"running": bool(pids), "pids": pids}
    except Exception as e:
        return {"running": False, "error": str(e), "pids": []}


def get_bindings(data_dir: Path) -> dict:
    """Load model-bindings.json from box data dir or fallback repo root."""
    cands = [data_dir / "model-bindings.json", REPO_ROOT / "model-bindings.json"]
    for c in cands:
        if c.is_file():
            try:
                return json.loads(c.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {"agents": {}}


def read_last_lines(path: Path, max_lines: int = 120) -> str:
    if not path.is_file():
        return ""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
        lines = txt.strip().splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception as e:
        return f"Error reading log: {e}"


def get_hop_server_status() -> dict:
    """Check hop-server process status."""
    try:
        r = subprocess.run(["pgrep", "-f", "hop-server.py"], capture_output=True, text=True)
        pids = [int(p) for p in r.stdout.strip().split() if p.isdigit()]
        return {"running": bool(pids), "pids": pids}
    except Exception as e:
        return {"running": False, "error": str(e), "pids": []}


def get_api_key_info(data_dir: Path) -> dict:
    key = os.environ.get("API_SERVER_KEY", "").strip()
    key_file = data_dir / ".api_key"
    if not key and key_file.is_file():
        try:
            key = key_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if not key:
        return {"configured": False, "preview": ""}
    if len(key) <= 8:
        preview = key[:2] + "****"
    else:
        preview = key[:4] + "****" + key[-4:]
    return {"configured": True, "preview": preview}


# --- host-wrap watchdog ------------------------------------------------------
# The sandbox supervisor may regenerate host-main.cjs from a pristine copy when
# it restarts the host, silently dropping the opengrok injection. The watchdog
# notices and re-wraps. Wrap-only: it NEVER rewrites model-bindings.json.

watchdog_state = {
    "enabled": False,
    "interval_sec": WATCHDOG_SEC,
    "last_check": None,
    "host_wrapped": None,
    "last_rewrap": None,
    "rewrap_count": 0,
    "last_error": None,
}


def host_wrap_state() -> str:
    if not DEFAULT_HOST.is_file():
        return "no-host"
    try:
        txt = DEFAULT_HOST.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "unreadable"
    if "opengrok-runtime" in txt:
        return "wrapped"
    if "createProtoSession" in txt:
        return "unwrapped"
    return "unknown"


def rewrap_host() -> tuple[bool, str]:
    """Re-inject the opengrok wrap into a stock host-main.cjs (wrap-only)."""
    if wrap_proto_session is None:
        return False, "wrap_proto_session not importable"
    data_dir = DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        for name in RUNTIME_FILES:  # the injected require() path must resolve
            src, dst = HERE / name, data_dir / name
            if src.is_file() and (not dst.is_file() or src.read_bytes() != dst.read_bytes()):
                shutil.copy2(src, dst)
        src_txt = DEFAULT_HOST.read_text(encoding="utf-8", errors="replace")
        wrapped = wrap_proto_session.wrap(src_txt, str(data_dir / "opengrok-runtime.cjs"))
    except Exception as e:
        return False, str(e)
    tmp = data_dir / "host-main.watchdog-check.cjs"
    try:
        stamp = time.strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(DEFAULT_HOST, data_dir / ("host-main.cjs.watchdog-%s" % stamp))
        tmp.write_text(wrapped, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        if r.returncode != 0:
            try:
                tmp.unlink()
            except OSError:
                pass
            return False, "node --check failed: " + (r.stderr or "")[:200]
        os.replace(tmp, DEFAULT_HOST)
    except Exception as e:
        return False, str(e)
    # Bounce so the running process loads the wrapped file (supervisor-safe).
    subprocess.run(["pkill", "-f", "host-main.cjs"], capture_output=True)
    return True, "re-wrapped host-main.cjs and bounced sand-host"


def watchdog_loop() -> None:
    watchdog_state["enabled"] = True
    print("[watchdog] host wrap watchdog started (every %ss)" % WATCHDOG_SEC, flush=True)
    while True:
        try:
            state = host_wrap_state()
            watchdog_state["last_check"] = _utc_now()
            if state == "wrapped":
                watchdog_state["host_wrapped"] = True
            elif state == "unwrapped":
                watchdog_state["host_wrapped"] = False
                print("[watchdog] host-main.cjs lost the opengrok wrap — re-wrapping", flush=True)
                ok, msg = rewrap_host()
                if ok:
                    watchdog_state["last_rewrap"] = _utc_now()
                    watchdog_state["rewrap_count"] += 1
                    watchdog_state["last_error"] = None
                    watchdog_state["host_wrapped"] = True
                else:
                    watchdog_state["last_error"] = msg
                print("[watchdog] " + ("ok: " if ok else "FAILED: ") + msg, flush=True)
            else:
                watchdog_state["host_wrapped"] = None
        except Exception as e:
            watchdog_state["last_error"] = str(e)
        time.sleep(WATCHDOG_SEC)


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenGrok 控制台 · Grok Bot 远程控制</title>
<style>
  :root{
    --bg:#0a0e17; --panel:#121826; --panel2:#0d1220; --border:#1e2942; --border-soft:#182136;
    --accent:#6366f1; --accent2:#818cf8; --text:#e6eaf2; --muted:#8b96ad;
    --ok:#34d399; --warn:#fbbf24; --err:#f87171; --info:#38bdf8;
    --radius:14px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{scroll-behavior:smooth}
  body{
    background:radial-gradient(1200px 500px at 80% -10%, rgba(99,102,241,.12), transparent 60%),
               radial-gradient(900px 400px at 0% 0%, rgba(56,189,248,.07), transparent 55%), var(--bg);
    color:var(--text); min-height:100vh;
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  }
  code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  .wrap{max-width:1040px;margin:0 auto;padding:20px 16px 64px}

  /* header */
  header{
    position:sticky;top:0;z-index:50;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
    padding:14px 4px;margin-bottom:18px;
    background:rgba(10,14,23,.82);backdrop-filter:blur(10px);border-bottom:1px solid var(--border-soft);
  }
  .brand h1{font-size:18px;font-weight:700;display:flex;align-items:center;gap:8px;letter-spacing:.2px}
  .brand p{font-size:12px;color:var(--muted);margin-top:2px}
  .badges{display:flex;gap:8px;flex-wrap:wrap}
  .badge{
    display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:999px;
    font-size:12px;font-weight:600;border:1px solid var(--border);color:var(--muted);background:var(--panel);
  }
  .badge .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
  .badge.on{color:var(--ok);border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.08)}
  .badge.off{color:var(--err);border-color:rgba(248,113,113,.4);background:rgba(248,113,113,.08)}
  .badge.idle{color:var(--muted)}

  /* stat cards */
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:18px}
  .stat{
    background:var(--panel);border:1px solid var(--border-soft);border-radius:var(--radius);
    padding:12px 14px;min-width:0;transition:border-color .15s;
  }
  .stat:hover{border-color:var(--border)}
  .stat .k{font-size:11px;color:var(--muted);font-weight:600;letter-spacing:.3px;text-transform:uppercase}
  .stat .v{font-size:13px;font-weight:700;margin-top:4px;word-break:break-all}
  .stat .v.c-info{color:var(--info)} .stat .v.c-warn{color:var(--warn)} .stat .v.c-ok{color:var(--ok)} .stat .v.c-err{color:var(--err)}

  /* tabs */
  .tabs{display:flex;gap:6px;margin-bottom:16px;overflow-x:auto;padding-bottom:2px}
  .tab{
    background:transparent;border:1px solid transparent;color:var(--muted);padding:8px 14px;border-radius:10px;
    font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .15s;
  }
  .tab:hover{color:var(--text);background:var(--panel)}
  .tab.active{color:#fff;background:linear-gradient(135deg,var(--accent),#4f46e5);box-shadow:0 4px 14px rgba(99,102,241,.35)}
  .panel{display:none;animation:fade .18s ease}
  .panel.active{display:block}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

  /* cards & forms */
  .card{
    background:var(--panel);border:1px solid var(--border-soft);border-radius:var(--radius);
    padding:20px;margin-bottom:16px;box-shadow:0 6px 24px rgba(0,0,0,.25);
  }
  .card h2{font-size:15px;font-weight:700;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
  .card .hint{font-size:12px;color:var(--muted);margin-bottom:14px}
  .fg{margin-bottom:16px}
  .fg label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:6px}
  .fg .help{font-size:11px;color:var(--muted);margin-top:5px;opacity:.85}
  input,select{
    width:100%;background:var(--panel2);border:1px solid var(--border);border-radius:10px;
    padding:10px 12px;color:var(--text);font-size:13px;outline:none;font-family:inherit;transition:border-color .15s, box-shadow .15s;
  }
  input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(99,102,241,.18)}
  select{appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);
    background-position:calc(100% - 18px) 50%,calc(100% - 13px) 50%;background-size:5px 5px;background-repeat:no-repeat;padding-right:34px}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .input-wrap{position:relative}
  .input-wrap .suffix{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--muted);cursor:pointer;font-size:12px;padding:4px 6px}

  /* switch */
  .switch{display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none}
  .switch input{display:none}
  .switch .track{width:40px;height:22px;border-radius:999px;background:#232c44;position:relative;transition:background .15s;flex:none}
  .switch .track::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#7d87a3;transition:all .15s}
  .switch input:checked + .track{background:var(--accent)}
  .switch input:checked + .track::after{left:21px;background:#fff}

  /* buttons */
  .btn{
    background:linear-gradient(135deg,var(--accent),#4f46e5);color:#fff;border:none;border-radius:10px;
    padding:10px 18px;font-size:13px;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:7px;
    transition:filter .15s,transform .05s,opacity .15s;
  }
  .btn:hover{filter:brightness(1.12)} .btn:active{transform:scale(.98)}
  .btn.ghost{background:var(--panel2);color:var(--text);border:1px solid var(--border)}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.danger{background:rgba(248,113,113,.12);color:var(--err);border:1px solid rgba(248,113,113,.35)}
  .btn.sm{padding:5px 11px;font-size:12px;border-radius:8px}
  .btn:disabled{opacity:.55;cursor:not-allowed}
  .btn .spin{width:12px;height:12px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .actions{display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;margin-top:4px}

  /* presets */
  .preset-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-bottom:18px}
  .preset{
    background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:10px 12px;cursor:pointer;
    transition:all .15s;text-align:left;color:var(--text);
  }
  .preset:hover{border-color:var(--accent);transform:translateY(-1px)}
  .preset .p-name{font-size:12px;font-weight:700;display:block}
  .preset .p-model{font-size:11px;color:var(--muted);display:block;margin-top:2px;word-break:break-all}
  .preset.own{border:1px solid rgba(52,211,153,.5);background:linear-gradient(135deg,rgba(52,211,153,.10),rgba(56,189,248,.06))}
  .preset.own .p-name{color:var(--ok)}

  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .chip{
    font-size:11px;padding:4px 10px;border-radius:999px;background:var(--panel2);color:var(--info);
    border:1px solid rgba(56,189,248,.25);cursor:pointer;transition:all .12s;
  }
  .chip:hover{background:var(--accent);color:#fff;border-color:var(--accent)}

  /* table */
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;padding:10px 12px;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;border-bottom:1px solid var(--border)}
  td{padding:11px 12px;border-bottom:1px solid var(--border-soft);vertical-align:middle}
  tbody tr{transition:background .12s}
  tbody tr:hover{background:rgba(255,255,255,.025)}
  .tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;background:rgba(99,102,241,.14);color:var(--accent2);margin:1px 2px}
  .tag.hi{background:rgba(251,191,36,.12);color:var(--warn)}
  .tag.star{background:rgba(52,211,153,.12);color:var(--ok)}

  /* modal */
  .modal-mask{
    position:fixed;inset:0;background:rgba(4,6,12,.7);backdrop-filter:blur(4px);
    display:none;justify-content:center;align-items:center;z-index:90;padding:16px;
  }
  .modal-mask.open{display:flex}
  .modal{
    width:100%;max-width:520px;background:var(--panel);border:1px solid var(--border);
    border-radius:16px;padding:22px;box-shadow:0 24px 64px rgba(0,0,0,.5);animation:fade .18s ease;
  }
  .modal h3{font-size:15px;font-weight:700;margin-bottom:16px}

  /* toasts */
  #toasts{position:fixed;top:16px;right:16px;z-index:100;display:flex;flex-direction:column;gap:8px;max-width:min(420px,calc(100vw - 32px))}
  .toast{
    display:flex;align-items:flex-start;gap:9px;padding:11px 14px;border-radius:12px;font-size:13px;font-weight:500;
    background:var(--panel);border:1px solid var(--border);box-shadow:0 10px 30px rgba(0,0,0,.45);animation:slidein .2s ease;
  }
  @keyframes slidein{from{opacity:0;transform:translateX(16px)}to{opacity:1;transform:none}}
  .toast.ok{border-color:rgba(52,211,153,.45);color:var(--ok)}
  .toast.err{border-color:rgba(248,113,113,.45);color:var(--err)}
  .toast.info{border-color:rgba(56,189,248,.45);color:var(--info)}
  .toast .x{margin-left:auto;cursor:pointer;opacity:.6;background:none;border:none;color:inherit;font-size:14px;padding:0 2px}
  .toast .x:hover{opacity:1}
  .toast .spin{width:13px;height:13px;border:2px solid rgba(56,189,248,.3);border-top-color:var(--info);border-radius:50%;animation:spin .7s linear infinite;flex:none;margin-top:2px}

  /* logs */
  .logbox{
    background:#070a12;border:1px solid var(--border-soft);border-radius:10px;padding:14px;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:#c3cbdd;
    height:340px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.55;
  }
  .lh-route{color:var(--info);font-weight:700}
  .lh-err{color:var(--err);font-weight:700}
  .lh-tool{color:var(--ok)}
  .lh-effort{color:var(--warn)}
  .lv-chip{font-size:11px;padding:3px 10px;border-radius:999px;border:1px solid var(--border);background:var(--panel2);color:var(--muted);cursor:pointer}
  .lv-chip.active{background:var(--accent);border-color:var(--accent);color:#fff}

  .wd-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
  .wd-item{background:var(--panel2);border:1px solid var(--border-soft);border-radius:10px;padding:10px 12px}
  .wd-item .k{font-size:11px;color:var(--muted);font-weight:600}
  .wd-item .v{font-size:13px;font-weight:700;margin-top:3px;word-break:break-all}

  @media (max-width:720px){
    .row2{grid-template-columns:1fr}
    .card{padding:16px}
    table,thead,tbody,tr,td{display:block;width:100%}
    thead{display:none}
    tr{border:1px solid var(--border-soft);border-radius:10px;margin-bottom:10px;padding:6px 4px}
    td{border:none;padding:5px 12px}
    td::before{content:attr(data-th);display:block;font-size:10px;color:var(--muted);font-weight:700;letter-spacing:.4px;text-transform:uppercase}
  }
</style>
</head>
<body>
<div id="toasts"></div>
<div class="wrap">
  <header>
    <div class="brand">
      <h1>⚡ OpenGrok 控制台</h1>
      <p>Grok Bot 远程控制 · 多渠道路由 · 深度推理管理</p>
    </div>
    <div class="badges">
      <span id="badgeHost" class="badge idle"><span class="dot"></span><span id="badgeHostT">host 检测中</span></span>
      <span id="badgeHop" class="badge idle"><span class="dot"></span><span id="badgeHopT">hop 检测中</span></span>
      <span id="badgeWrap" class="badge idle"><span class="dot"></span><span id="badgeWrapT">包装 检测中</span></span>
    </div>
  </header>

  <div class="stats">
    <div class="stat"><div class="k">Tailscale IP</div><div class="v mono" id="statIp">-</div></div>
    <div class="stat"><div class="k">当前模型</div><div class="v c-info" id="statModel">-</div></div>
    <div class="stat"><div class="k">推理配置</div><div class="v c-warn" id="statEffort">-</div></div>
    <div class="stat"><div class="k">上游地址</div><div class="v" id="statUpstream">-</div></div>
    <div class="stat"><div class="k">API Key</div><div class="v" id="statKey">-</div></div>
    <div class="stat"><div class="k">包装看门狗</div><div class="v c-ok" id="statWatchdog">-</div></div>
  </div>

  <nav class="tabs">
    <button class="tab active" data-tab="primary" onclick="switchTab('primary',this)">🌟 主力模型</button>
    <button class="tab" data-tab="agents" onclick="switchTab('agents',this)">🤖 Agent 路由</button>
    <button class="tab" data-tab="adaptive" onclick="switchTab('adaptive',this)">⚡ 自适应深度</button>
    <button class="tab" data-tab="system" onclick="switchTab('system',this)">🛠️ 系统运维</button>
    <button class="tab" data-tab="logs" onclick="switchTab('logs',this)">📜 实时日志</button>
  </nav>

  <!-- 主力模型 -->
  <section id="tab-primary" class="panel active">
    <div class="card">
      <h2>渠道预设</h2>
      <div class="hint">点击快速填充。自有渠道不回写仓库——从沙箱当前配置实时读取。</div>
      <div class="preset-grid">
        <button class="preset own" onclick="useCurrentConfig()"><span class="p-name">✨ 我的自有渠道</span><span class="p-model">读取沙箱当前配置</span></button>
        <button class="preset" onclick="setPreset('xai-grok4')"><span class="p-name">⚡ xAI Grok-4.6</span><span class="p-model">grok-4.6</span></button>
        <button class="preset" onclick="setPreset('xai-grok2')"><span class="p-name">⚡ xAI Grok-2</span><span class="p-model">grok-2-latest</span></button>
        <button class="preset" onclick="setPreset('deepseek-r1')"><span class="p-name">🧠 DeepSeek-R1</span><span class="p-model">deepseek-reasoner</span></button>
        <button class="preset" onclick="setPreset('deepseek-v3')"><span class="p-name">🚀 DeepSeek-V3</span><span class="p-model">deepseek-chat</span></button>
        <button class="preset" onclick="setPreset('claude-37')"><span class="p-name">🔮 Claude 3.7</span><span class="p-model">claude-3-7-sonnet</span></button>
        <button class="preset" onclick="setPreset('gemini-25')"><span class="p-name">💎 Gemini 2.5</span><span class="p-model">gemini-2.5-pro</span></button>
        <button class="preset" onclick="setPreset('glm-flash')"><span class="p-name">🇨🇳 智谱 GLM</span><span class="p-model">glm-5.3-flash</span></button>
        <button class="preset" onclick="setPreset('openai-4o')"><span class="p-name">🟢 GPT-4o</span><span class="p-model">gpt-4o</span></button>
        <button class="preset" onclick="setPreset('ollama')"><span class="p-name">💻 本地 Ollama</span><span class="p-model">qwen2.5-coder</span></button>
      </div>

      <div class="fg">
        <label>上游 API 根地址</label>
        <input id="inUpstream" placeholder="https://api.x.ai/v1 或 https://api.deepseek.com" autocomplete="off">
      </div>
      <div class="fg">
        <label>API Key</label>
        <div class="input-wrap">
          <input type="password" id="inKey" placeholder="留空则保持现有 Key 不变" autocomplete="new-password" style="padding-right:52px">
          <button type="button" class="suffix" onclick="toggleKeyVis()">显示</button>
        </div>
        <div class="help">仅存于沙箱 .api_key（权限 600），由 hop 注入，绝不进入绑定文件或仓库。</div>
      </div>
      <div class="fg">
        <label>模型标识符（Model Slug）</label>
        <div style="display:flex;gap:8px">
          <input id="inModel" placeholder="grok-4.6 / deepseek-reasoner / claude-3-7-sonnet" list="modelList" style="flex:1">
          <button class="btn ghost sm" id="btnFetchModels" onclick="fetchUpstreamModels(this)">🔍 拉取上游模型</button>
        </div>
        <datalist id="modelList"></datalist>
        <div class="chips" id="modelChips"></div>
      </div>
      <div class="row2">
        <div class="fg">
          <label>推理深度（Reasoning Effort）</label>
          <select id="selEffort">
            <option value="high">high · 深度思考（自有渠道默认）</option>
            <option value="xhigh">xhigh / max · 满血思考</option>
            <option value="medium">medium · 中度思考</option>
            <option value="low">low · 轻量思考</option>
          </select>
        </div>
        <div class="fg">
          <label>极速模式</label>
          <label class="switch" style="margin-top:8px">
            <input type="checkbox" id="swFast"><span class="track"></span>
            <span style="font-size:12px;color:var(--muted)">Fast Lane · 关闭多余思考</span>
          </label>
        </div>
      </div>
      <div class="actions">
        <button class="btn ghost" id="btnTest" onclick="testConnection(this)">🧪 测试连通性</button>
        <button class="btn" id="btnSave" onclick="saveAndApply(this)">💾 保存并应用</button>
      </div>
    </div>
  </section>

  <!-- Agent 路由 -->
  <section id="tab-agents" class="panel">
    <div class="card">
      <h2>Agent 路由规则 <button class="btn sm" onclick="openAgentModal(null)">＋ 新增绑定</button></h2>
      <div class="hint">Grok Bot 每个 Agent / 对话携带唯一 UUID（会话日志中可观察）。未命中的 Agent 回退到全局默认 <code>*</code>。</div>
      <table>
        <thead><tr><th>Agent</th><th>别名</th><th>模型</th><th>推理</th><th style="width:130px">操作</th></tr></thead>
        <tbody id="agentsBody"><tr><td colspan="5" style="text-align:center;color:var(--muted)">加载中…</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- 自适应深度 -->
  <section id="tab-adaptive" class="panel">
    <div class="card">
      <h2>上下文自适应推理（effortWhen）</h2>
      <div class="hint">最近消息命中关键词时动态调整本回合推理深度：架构/重构走 high，查状态/diff 走 medium。</div>
      <div class="fg">
        <label>触发 medium 的关键词（英文逗号分隔）</label>
        <input id="inEffMed" placeholder="git diff, status, test, review">
      </div>
      <div class="fg">
        <label>触发 high 的关键词（英文逗号分隔）</label>
        <input id="inEffHigh" placeholder="refactor, architect, audit, security, math, bugfix">
      </div>
      <div class="actions"><button class="btn" id="btnAdaptive" onclick="saveAdaptiveRules(this)">💾 保存规则</button></div>
    </div>
  </section>

  <!-- 系统运维 -->
  <section id="tab-system" class="panel">
    <div class="card">
      <h2>包装看门狗 <span id="wdBadge" class="badge idle" style="font-size:11px"><span class="dot"></span>检测中</span></h2>
      <div class="hint">沙箱守护进程重启 host 时会用原始副本覆盖 host-main.cjs 导致包装丢失。看门狗定时检测并自动重包装（不动绑定配置）。</div>
      <div class="wd-grid">
        <div class="wd-item"><div class="k">状态</div><div class="v" id="wdState">-</div></div>
        <div class="wd-item"><div class="k">检测间隔</div><div class="v" id="wdInterval">-</div></div>
        <div class="wd-item"><div class="k">上次检查</div><div class="v mono" id="wdLastCheck" style="font-size:11px">-</div></div>
        <div class="wd-item"><div class="k">上次重包装</div><div class="v mono" id="wdLastRewrap" style="font-size:11px">-</div></div>
        <div class="wd-item"><div class="k">累计重包装</div><div class="v" id="wdCount">-</div></div>
      </div>
      <div id="wdErrorBox" style="display:none" class="fg">
        <label>最近错误</label>
        <div class="logbox" style="height:auto;max-height:90px;color:var(--err)" id="wdError"></div>
      </div>
    </div>
    <div class="card">
      <h2>沙箱操作</h2>
      <div class="actions" style="justify-content:flex-start">
        <button class="btn ghost" onclick="restartSandHost(this)">🔄 热重启 sand-host</button>
        <button class="btn ghost" onclick="reinstallHost(this)">🛠️ 重新注入补丁</button>
        <button class="btn ghost" onclick="runDoctor(this)">🩺 Doctor 诊断</button>
        <button class="btn ghost" onclick="refreshStatus(true)">⚡ 刷新状态</button>
      </div>
      <div class="fg" id="doctorBox" style="display:none;margin-top:16px">
        <label>诊断输出</label>
        <div class="logbox" style="height:220px" id="doctorOut"></div>
      </div>
    </div>
  </section>

  <!-- 日志 -->
  <section id="tab-logs" class="panel">
    <div class="card">
      <h2>会话与路由日志
        <span style="display:flex;gap:8px">
          <button class="btn ghost sm" onclick="clearLogs()">🗑️ 清空</button>
          <button class="btn ghost sm" onclick="fetchLogs()">↻ 刷新</button>
        </span>
      </h2>
      <div class="hint mono">/tmp/opengrok-session.log</div>
      <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;align-items:center">
        <input id="logFilter" placeholder="🔍 过滤关键词（route / error / SendToUser / reasoning）…" oninput="applyLogFilter()" style="flex:1;min-width:200px">
        <span class="lv-chip active" data-lv="all" onclick="setLogLevel('all',this)">全部</span>
        <span class="lv-chip" data-lv="route" onclick="setLogLevel('route',this)">路由</span>
        <span class="lv-chip" data-lv="err" onclick="setLogLevel('err',this)">错误</span>
        <span class="lv-chip" data-lv="tool" onclick="setLogLevel('tool',this)">工具</span>
        <span class="lv-chip" data-lv="effort" onclick="setLogLevel('effort',this)">推理</span>
        <label class="switch"><input type="checkbox" id="autoScroll" checked><span class="track"></span><span style="font-size:12px;color:var(--muted)">滚到底部</span></label>
      </div>
      <div class="logbox" id="logBox">载入中…</div>
    </div>
  </section>
</div>

<!-- Agent modal -->
<div class="modal-mask" id="agentModal" onclick="if(event.target===this)closeAgentModal()">
  <div class="modal">
    <h3 id="modalTitle">新增 Agent 绑定</h3>
    <div class="fg" id="agentSelectRow">
      <label>选择 Agent（已配置 / 日志中观察到的 UUID）</label>
      <select id="selAgent" onchange="onAgentSelect()"></select>
    </div>
    <div class="fg" id="agentCustomRow" style="display:none">
      <label>手动输入 Agent UUID</label>
      <input id="inAgentCustom" class="mono" placeholder="00000000-0000-4000-8000-000000000001">
    </div>
    <div class="fg" id="agentFixedRow" style="display:none">
      <label>Agent</label>
      <input id="inAgentFixed" class="mono" readonly style="opacity:.75">
    </div>
    <div class="fg">
      <label>别名</label>
      <input id="inAgentName" placeholder="例如：架构评审专家">
    </div>
    <div class="fg">
      <label>绑定模型</label>
      <input id="inAgentModel" list="modelList" placeholder="grok-4.6 / claude-3-7-sonnet">
    </div>
    <div class="fg">
      <label>推理深度</label>
      <select id="selAgentEffort">
        <option value="high">high · 深度思考</option>
        <option value="xhigh">xhigh / max</option>
        <option value="medium">medium</option>
        <option value="low">low</option>
      </select>
    </div>
    <div class="actions">
      <button class="btn ghost" onclick="closeAgentModal()">取消</button>
      <button class="btn" id="btnAgentSave" onclick="saveAgentFromModal(this)">确认保存</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let globalBindings = {agents:{}};
let recentAgentIds = [];
let rawLogText = '';
let logLevel = 'all';
let editingAgentKey = null;

const PRESETS = {
  'xai-grok4':  {url:'https://api.x.ai/v1', model:'grok-4.6', effort:'high', desc:'xAI Grok-4.6'},
  'xai-grok2':  {url:'https://api.x.ai/v1', model:'grok-2-latest', effort:'high', desc:'xAI Grok-2'},
  'deepseek-r1':{url:'https://api.deepseek.com', model:'deepseek-reasoner', effort:'high', desc:'DeepSeek-R1'},
  'deepseek-v3':{url:'https://api.deepseek.com', model:'deepseek-chat', effort:'high', desc:'DeepSeek-V3'},
  'claude-37':  {url:'https://openrouter.ai/api', model:'anthropic/claude-3.7-sonnet', effort:'high', desc:'Claude 3.7 Sonnet'},
  'gemini-25':  {url:'https://generativelanguage.googleapis.com', model:'gemini-2.5-pro', effort:'high', desc:'Gemini 2.5 Pro'},
  'glm-flash':  {url:'https://open.bigmodel.cn/api/paas', model:'glm-5.3-flash', effort:'high', desc:'智谱 GLM-5.3'},
  'openai-4o':  {url:'https://api.openai.com', model:'gpt-4o', effort:'high', desc:'OpenAI GPT-4o'},
  'ollama':     {url:'http://127.0.0.1:11434', model:'qwen2.5-coder', effort:'low', desc:'本地 Ollama'},
};

/* ---------- toast ---------- */
function toast(type, msg, stickyMs){
  const box = $('toasts');
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  const icon = type==='ok' ? '✅' : type==='err' ? '⛔' : '';
  t.innerHTML = (type==='info' ? '<span class="spin"></span>' : '<span>'+icon+'</span>') + '<span style="flex:1"></span>';
  t.querySelector('span[style]').textContent = msg;
  const x = document.createElement('button');
  x.className = 'x'; x.textContent = '✕';
  x.onclick = () => t.remove();
  t.appendChild(x);
  box.appendChild(t);
  const ttl = stickyMs != null ? stickyMs : (type==='err' ? 9000 : type==='ok' ? 4000 : 0);
  if (ttl > 0) setTimeout(() => t.remove(), ttl);
  return t;
}

/* ---------- helpers ---------- */
async function api(path, body){
  const opt = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const res = await fetch(path, opt);
  return res.json();
}
function setBusy(btn, on, busyText){
  if (!btn) return;
  if (on){
    btn.dataset.orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>' + (busyText || '处理中…');
  } else {
    btn.disabled = false;
    if (btn.dataset.orig) btn.innerHTML = btn.dataset.orig;
  }
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function shortId(k){ return k === '*' ? '*' : (k.length > 18 ? k.slice(0,8) + '…' + k.slice(-6) : k); }

/* ---------- tabs ---------- */
function switchTab(name, btn){
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'logs') fetchLogs();
}

/* ---------- primary tab ---------- */
function setPreset(key){
  const p = PRESETS[key];
  if (!p) return;
  $('inUpstream').value = p.url;
  $('inModel').value = p.model;
  $('selEffort').value = p.effort || 'high';
  $('swFast').checked = false;
  toast('ok', '已填充：' + p.desc + '（推理 ' + (p.effort || 'high') + '）');
}

function useCurrentConfig(){
  const a = (globalBindings.agents && (globalBindings.agents['*'] || Object.values(globalBindings.agents)[0])) || {};
  if (!a.upstream && !a.modelId){
    toast('err', '沙箱当前没有已保存的渠道配置，请先手动填写并保存一次');
    return;
  }
  if (a.upstream) $('inUpstream').value = a.upstream;
  if (a.modelId) $('inModel').value = a.modelId;
  const eff = (a.parameters || []).find(p => p.id === 'effort');
  $('selEffort').value = eff ? eff.value : 'high';
  const fast = (a.parameters || []).find(p => p.id === 'fast');
  $('swFast').checked = !!(fast && (fast.value === true || fast.value === 'true'));
  toast('ok', '已载入沙箱当前配置：' + (a.modelId || '-'));
}

function toggleKeyVis(){
  const i = $('inKey');
  const b = i.parentElement.querySelector('.suffix');
  if (i.type === 'password'){ i.type = 'text'; b.textContent = '隐藏'; }
  else { i.type = 'password'; b.textContent = '显示'; }
}

async function fetchUpstreamModels(btn){
  const upstream = $('inUpstream').value.trim();
  const apiKey = $('inKey').value.trim();
  if (!upstream){ toast('err', '请先输入上游 API 根地址'); return; }
  setBusy(btn, true, '拉取中…');
  const t = toast('info', '正在获取上游模型列表…');
  try{
    const d = await api('/api/models', {upstream, apiKey});
    t.remove();
    if (d.ok && Array.isArray(d.models) && d.models.length){
      $('modelList').innerHTML = '';
      $('modelChips').innerHTML = '';
      d.models.forEach(m => {
        const o = document.createElement('option'); o.value = m; $('modelList').appendChild(o);
        const c = document.createElement('span'); c.className = 'chip'; c.textContent = m;
        c.onclick = () => { $('inModel').value = m; };
        $('modelChips').appendChild(c);
      });
      toast('ok', '获取到 ' + d.models.length + ' 个可用模型（点击芯片快速选择）');
    } else {
      toast('err', '获取失败：' + (d.error || '未返回可用模型'));
    }
  }catch(e){ t.remove(); toast('err', '请求异常：' + e.message); }
  setBusy(btn, false);
}

async function testConnection(btn){
  const upstream = $('inUpstream').value.trim();
  const model = $('inModel').value.trim();
  const apiKey = $('inKey').value.trim();
  if (!upstream || !model){ toast('err', '请先填写上游地址和模型标识'); return; }
  setBusy(btn, true, '探测中…');
  const t = toast('info', '正在向上游发送测试探针…');
  try{
    const d = await api('/api/test', {upstream, model, apiKey});
    t.remove();
    if (d.ok) toast('ok', '探测成功：' + (d.message || '模型响应正常'));
    else toast('err', '探测失败：' + (d.error || '无法连接'));
  }catch(e){ t.remove(); toast('err', '请求异常：' + e.message); }
  setBusy(btn, false);
}

async function saveAndApply(btn){
  const upstream = $('inUpstream').value.trim();
  const model = $('inModel').value.trim();
  const apiKey = $('inKey').value.trim();
  const effort = $('selEffort').value;
  const fast = $('swFast').checked;
  if (!upstream || !model){ toast('err', '请完整填写上游根地址和模型标识'); return; }
  if (!confirm('保存将热重启 sand-host（进行中的会话会被中断）。继续吗？')) return;
  setBusy(btn, true, '保存并重启中…');
  const t = toast('info', '正在写入配置并热重启沙箱服务…');
  try{
    const d = await api('/api/save', {upstream, model, apiKey, effort, fast});
    t.remove();
    if (d.ok){
      toast('ok', '配置已应用，宿主进程已重启');
      $('inKey').value = '';
      setTimeout(() => refreshStatus(false), 1500);
    } else toast('err', '保存失败：' + (d.error || '未知错误'));
  }catch(e){ t.remove(); toast('err', '保存异常：' + e.message); }
  setBusy(btn, false);
}

/* ---------- agents tab ---------- */
function paramOf(a, id){ const p = (a.parameters || []).find(p => p.id === id); return p ? p.value : null; }

function renderAgents(){
  const agents = globalBindings.agents || {};
  const tbody = $('agentsBody');
  const keys = Object.keys(agents);
  if (!keys.length){
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted)">暂无规则（全部走全局默认）</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  keys.sort((a,b) => (a === '*' ? -1 : b === '*' ? 1 : a.localeCompare(b))).forEach(k => {
    const a = agents[k] || {};
    const tr = document.createElement('tr');
    const eff = paramOf(a, 'effort') || 'default';
    const thinking = paramOf(a, 'thinking');
    const star = k === '*';
    tr.innerHTML =
      '<td data-th="Agent"><code>' + (star ? '★ 全局默认 (*)' : esc(shortId(k))) + '</code>' + (star ? '' : '<div style="font-size:10px;color:var(--muted);word-break:break-all">' + esc(k) + '</div>') + '</td>' +
      '<td data-th="别名">' + esc(a.name || '-') + '</td>' +
      '<td data-th="模型"><b style="color:var(--info)">' + esc(a.modelId || '-') + '</b></td>' +
      '<td data-th="推理"><span class="tag' + (eff === 'high' || eff === 'xhigh' ? ' hi' : '') + '">effort=' + esc(eff) + '</span>' +
        (thinking ? '<span class="tag">thinking=' + esc(String(thinking)) + '</span>' : '') + '</td>' +
      '<td data-th="操作"><button class="btn ghost sm" onclick="openAgentModal(\'' + esc(k) + '\')">编辑</button> ' +
        (star ? '' : '<button class="btn danger sm" onclick="deleteAgent(\'' + esc(k) + '\')">删除</button>') + '</td>';
    tbody.appendChild(tr);
  });
}

function openAgentModal(editKey){
  editingAgentKey = editKey;
  $('agentCustomRow').style.display = 'none';
  $('inAgentCustom').value = '';
  if (editKey){
    $('modalTitle').textContent = '编辑 Agent 绑定';
    $('agentSelectRow').style.display = 'none';
    $('agentFixedRow').style.display = 'block';
    $('inAgentFixed').value = editKey === '*' ? '★ 全局默认 (*)' : editKey;
    const a = (globalBindings.agents || {})[editKey] || {};
    $('inAgentName').value = a.name || '';
    $('inAgentModel').value = a.modelId || '';
    $('selAgentEffort').value = paramOf(a, 'effort') || 'high';
  } else {
    $('modalTitle').textContent = '新增 Agent 绑定';
    $('agentSelectRow').style.display = 'block';
    $('agentFixedRow').style.display = 'none';
    buildAgentSelect();
    $('inAgentName').value = '';
    $('inAgentModel').value = '';
    $('selAgentEffort').value = 'high';
  }
  $('agentModal').classList.add('open');
}

function buildAgentSelect(){
  const sel = $('selAgent');
  const agents = globalBindings.agents || {};
  sel.innerHTML = '';
  const known = new Set(Object.keys(agents));
  Object.keys(agents).forEach(k => {
    const o = document.createElement('option');
    o.value = k;
    o.textContent = k === '*' ? '★ 全局默认 (*)' : (agents[k].name ? agents[k].name + ' · ' : '') + shortId(k);
    sel.appendChild(o);
  });
  recentAgentIds.filter(u => !known.has(u)).forEach(u => {
    const o = document.createElement('option');
    o.value = u;
    o.textContent = '🛰 日志观察到 · ' + shortId(u);
    sel.appendChild(o);
  });
  const custom = document.createElement('option');
  custom.value = '__custom';
  custom.textContent = '✏️ 手动输入新 UUID…';
  sel.appendChild(custom);
  if (sel.options.length && sel.value === '') sel.selectedIndex = 0;
  onAgentSelect();
}

function onAgentSelect(){
  $('agentCustomRow').style.display = $('selAgent').value === '__custom' ? 'block' : 'none';
}

function closeAgentModal(){ $('agentModal').classList.remove('open'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAgentModal(); });

async function saveAgentFromModal(btn){
  let key;
  if (editingAgentKey) key = editingAgentKey;
  else key = $('selAgent').value === '__custom' ? $('inAgentCustom').value.trim() : $('selAgent').value;
  const name = $('inAgentName').value.trim();
  const model = $('inAgentModel').value.trim();
  const effort = $('selAgentEffort').value;
  if (!key || !model){ toast('err', '请选择 Agent 并填写模型名称'); return; }
  if (!globalBindings.agents) globalBindings.agents = {};
  const prev = globalBindings.agents[key] || {};
  const star = globalBindings.agents['*'] || {};
  const upstream = prev.upstream || star.upstream || $('inUpstream').value.trim() || 'https://api.x.ai/v1';
  globalBindings.agents[key] = Object.assign({}, prev, {
    name: name || model,
    modelId: model,
    provider: prev.provider || 'custom',
    hopBaseUrl: prev.hopBaseUrl || 'http://127.0.0.1:18790/v1',
    upstream: upstream,
    parameters: [{id:'effort', value:effort}, {id:'thinking', value:'true'}],
  });
  closeAgentModal();
  await syncFullBindings(btn);
}

async function deleteAgent(key){
  if (!confirm('确定删除 Agent 绑定 [' + key + '] 吗？')) return;
  delete (globalBindings.agents || {})[key];
  await syncFullBindings(null);
}

async function syncFullBindings(btn){
  setBusy(btn, true, '同步中…');
  const t = toast('info', '正在同步 Agent 路由配置…');
  try{
    const d = await api('/api/save-bindings', {bindings: globalBindings});
    t.remove();
    if (d.ok){ toast('ok', 'Agent 路由配置已更新'); refreshStatus(false); }
    else toast('err', '保存失败：' + (d.error || '未知错误'));
  }catch(e){ t.remove(); toast('err', '请求异常：' + e.message); }
  setBusy(btn, false);
}

/* ---------- adaptive ---------- */
async function saveAdaptiveRules(btn){
  const med = $('inEffMed').value.split(',').map(s => s.trim()).filter(Boolean);
  const hi = $('inEffHigh').value.split(',').map(s => s.trim()).filter(Boolean);
  const agent = (globalBindings.agents && (globalBindings.agents['*'] || Object.values(globalBindings.agents)[0])) || null;
  if (!agent){ toast('err', '尚无全局绑定可挂载规则'); return; }
  agent.effortWhen = {};
  if (med.length) agent.effortWhen.medium = med;
  if (hi.length) agent.effortWhen.high = hi;
  await syncFullBindings(btn);
}

/* ---------- system ops ---------- */
async function restartSandHost(btn){
  if (!confirm('热重启 sand-host 会中断进行中的会话。继续吗？')) return;
  setBusy(btn, true, '重启中…');
  try{
    const d = await api('/api/restart-host', {});
    if (d.ok){ toast('ok', 'sand-host 已重启'); setTimeout(() => refreshStatus(false), 2000); }
    else toast('err', '重启失败：' + (d.error || ''));
  }catch(e){ toast('err', '重启异常：' + e.message); }
  setBusy(btn, false);
}

async function reinstallHost(btn){
  if (!confirm('重新注入 OpenGrok 补丁（watchdog 也会自动处理包装丢失）。继续吗？')) return;
  setBusy(btn, true, '注入中…');
  try{
    const d = await api('/api/install', {});
    if (d.ok){ toast('ok', '补丁已重新注入'); setTimeout(() => refreshStatus(false), 2000); }
    else toast('err', '注入失败：' + (d.error || d.output || ''));
  }catch(e){ toast('err', '请求异常：' + e.message); }
  setBusy(btn, false);
}

async function runDoctor(btn){
  setBusy(btn, true, '诊断中…');
  try{
    const d = await api('/api/doctor');
    $('doctorBox').style.display = 'block';
    $('doctorOut').textContent = d.output || '诊断完成';
    toast(d.ok ? 'ok' : 'err', d.ok ? '诊断通过' : '诊断发现异常，请查看输出');
  }catch(e){ toast('err', '诊断异常：' + e.message); }
  setBusy(btn, false);
}

/* ---------- logs ---------- */
function lineKind(l){
  const s = l.toLowerCase();
  if (s.includes('error')) return 'err';
  if (s.includes('route ')) return 'route';
  if (s.includes('tool_call') || s.includes('sendtouser')) return 'tool';
  if (s.includes('effortwhen') || s.includes('params=')) return 'effort';
  return 'other';
}
function setLogLevel(lv, chip){
  logLevel = lv;
  document.querySelectorAll('.lv-chip').forEach(c => c.classList.toggle('active', c === chip));
  applyLogFilter();
}
function applyLogFilter(){
  const filter = $('logFilter').value.toLowerCase().trim();
  const lines = rawLogText.split('\n');
  const filtered = lines.filter(l => {
    if (logLevel !== 'all' && lineKind(l) !== logLevel) return false;
    return !filter || l.toLowerCase().includes(filter);
  });
  $('logBox').innerHTML = filtered.map(line => {
    let safe = esc(line);
    const k = lineKind(line);
    if (k === 'route') return '<span class="lh-route">' + safe + '</span>';
    if (k === 'err') return '<span class="lh-err">' + safe + '</span>';
    if (k === 'tool') return '<span class="lh-tool">' + safe + '</span>';
    if (k === 'effort') return '<span class="lh-effort">' + safe + '</span>';
    return safe;
  }).join('\n') || '(无匹配日志)';
  if ($('autoScroll').checked) $('logBox').scrollTop = $('logBox').scrollHeight;
}
async function fetchLogs(){
  try{
    const d = await api('/api/status');
    if (d.session_log !== undefined){ rawLogText = d.session_log || ''; applyLogFilter(); }
  }catch(e){}
}
async function clearLogs(){
  if (!confirm('确定清空会话日志吗？')) return;
  try{
    await api('/api/clear-log', {});
    rawLogText = ''; applyLogFilter();
    toast('ok', '日志已清空');
  }catch(e){}
}

/* ---------- status ---------- */
function setBadge(el, txtEl, state, text){
  el.className = 'badge ' + state;
  txtEl.textContent = text;
}
function fmtTime(iso){
  if (!iso) return '从未';
  try{ return iso.replace('T', ' ').slice(5, 19); }catch(e){ return iso; }
}

async function refreshStatus(syncForm){
  try{
    const d = await api('/api/status');

    $('statIp').textContent = d.tailscale_ip || '127.0.0.1';

    if (d.sand_host && d.sand_host.running)
      setBadge($('badgeHost'), $('badgeHostT'), 'on', 'sand-host · PID ' + d.sand_host.pids.join(','));
    else setBadge($('badgeHost'), $('badgeHostT'), 'off', 'sand-host 未运行');

    if (d.hop_server && d.hop_server.running)
      setBadge($('badgeHop'), $('badgeHopT'), 'on', 'hop 代理 · PID ' + d.hop_server.pids.join(','));
    else setBadge($('badgeHop'), $('badgeHopT'), 'off', 'hop 代理未启动');

    if (d.wrapped === true) setBadge($('badgeWrap'), $('badgeWrapT'), 'on', '包装 已注入');
    else if (d.wrapped === false) setBadge($('badgeWrap'), $('badgeWrapT'), 'off', '包装 缺失 ⚠️');
    else setBadge($('badgeWrap'), $('badgeWrapT'), 'idle', '包装 检测中');

    globalBindings = d.bindings || {agents:{}};
    const agent = (globalBindings.agents && (globalBindings.agents['*'] || Object.values(globalBindings.agents)[0])) || {};
    $('statModel').textContent = agent.modelId || '未配置';
    const eff = paramOf(agent, 'effort');
    const thk = paramOf(agent, 'thinking');
    const fst = paramOf(agent, 'fast');
    let effText = eff ? 'effort=' + eff : 'default';
    if (thk) effText += ' · thinking=' + thk;
    if (fst === 'true' || fst === true) effText += ' · fast';
    $('statEffort').textContent = effText;
    $('statUpstream').textContent = agent.upstream || '未配置';
    $('statKey').textContent = (d.api_key_info && d.api_key_info.configured) ? '已配置 (' + d.api_key_info.preview + ')' : '未设置 ⚠️';
    $('statKey').className = 'v ' + ((d.api_key_info && d.api_key_info.configured) ? 'c-ok' : 'c-err');

    const wd = d.watchdog || {};
    if (wd.enabled){
      $('statWatchdog').textContent = '运行中 · 重包装 ' + (wd.rewrap_count || 0) + ' 次';
      $('statWatchdog').className = 'v c-ok';
      $('wdBadge').className = 'badge on';
      $('wdBadge').innerHTML = '<span class="dot"></span>看门狗运行中';
      $('wdState').textContent = '运行中';
      $('wdInterval').textContent = (wd.interval_sec || '-') + 's';
      $('wdLastCheck').textContent = fmtTime(wd.last_check);
      $('wdLastRewrap').textContent = fmtTime(wd.last_rewrap);
      $('wdCount').textContent = String(wd.rewrap_count || 0);
      $('wdErrorBox').style.display = wd.last_error ? 'block' : 'none';
      $('wdError').textContent = wd.last_error || '';
    } else {
      $('statWatchdog').textContent = '未启用';
      $('statWatchdog').className = 'v c-err';
      $('wdBadge').className = 'badge off';
      $('wdBadge').innerHTML = '<span class="dot"></span>看门狗未启用';
      $('wdState').textContent = '未启用（非沙箱环境或已禁用）';
    }

    renderAgents();

    if (syncForm || !$('inUpstream').value){
      if (agent.upstream) $('inUpstream').value = agent.upstream;
      if (agent.modelId) $('inModel').value = agent.modelId;
      if (eff) $('selEffort').value = eff;
      $('swFast').checked = (fst === true || fst === 'true');
      if (agent.effortWhen){
        $('inEffMed').value = (agent.effortWhen.medium || []).join(', ');
        $('inEffHigh').value = (agent.effortWhen.high || []).join(', ');
      }
    }

    if (d.session_log !== undefined){ rawLogText = d.session_log || ''; applyLogFilter(); }
  }catch(e){ /* tailnet hiccup — next tick retries */ }
}

async function fetchRecentAgents(){
  try{
    const d = await api('/api/agents');
    if (d.ok) recentAgentIds = d.recent_ids || [];
  }catch(e){}
}

refreshStatus(true);
fetchRecentAgents();
setInterval(fetchRecentAgents, 30000);
setInterval(() => { if (!document.hidden) refreshStatus(false); }, 6000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: N802
        pass

    def _json(self, code: int, data: dict) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, code: int, html_str: str) -> None:
        payload = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            self._html(200, PAGE_HTML)
            return

        if self.path == "/api/status":
            data_dir = DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT
            # Wrap state comes from the watchdog cache when available — reading
            # the 25MB host file on every 6s poll is wasteful.
            wrapped = watchdog_state.get("host_wrapped")
            if wrapped is None and not watchdog_state.get("enabled"):
                wrapped = host_wrap_state() == "wrapped"

            self._json(200, {
                "tailscale_ip": get_tailscale_ip(),
                "sand_host": get_sand_host_status(),
                "hop_server": get_hop_server_status(),
                "api_key_info": get_api_key_info(data_dir),
                "bindings": get_bindings(data_dir),
                "wrapped": wrapped,
                "watchdog": dict(watchdog_state),
                "session_log": read_last_lines(SESSION_LOG, 120),
            })
            return

        if self.path == "/api/agents":
            # Dropdown data for the binding modal: configured agents + agent
            # UUIDs recently observed in the routing log (one-click binding).
            data_dir = DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT
            agents = (get_bindings(data_dir).get("agents") or {})
            recent, seen = [], set()
            if SESSION_LOG.is_file():
                try:
                    txt = SESSION_LOG.read_text(encoding="utf-8", errors="replace")
                    for m in re.finditer(
                        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", txt
                    ):
                        u = m.group(0).lower()
                        if u not in seen:
                            seen.add(u)
                            recent.append(u)
                except Exception:
                    pass
            self._json(200, {"ok": True, "agents": agents, "recent_ids": recent[-20:]})
            return

        if self.path == "/api/doctor":
            try:
                r = subprocess.run([sys.executable, str(HERE / "doctor.py")], capture_output=True, text=True, timeout=10)
                out = (r.stdout or "") + (r.stderr or "")
                self._json(200, {"ok": r.returncode == 0, "output": out})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            length = 0
        body = self.rfile.read(length) if length else b"{}"
        try:
            req_data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req_data = {}

        if self.path == "/api/clear-log":
            try:
                if SESSION_LOG.is_file():
                    SESSION_LOG.write_text("", encoding="utf-8")
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/models":
            upstream = req_data.get("upstream", "").rstrip("/")
            api_key = req_data.get("apiKey", "") or os.environ.get("API_SERVER_KEY", "")
            if not api_key:
                key_file = (DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT) / ".api_key"
                if key_file.is_file():
                    try:
                        api_key = key_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass
            if not upstream:
                self._json(400, {"ok": False, "error": "upstream required"})
                return

            models_url = (upstream + "/models") if upstream.endswith("/v1") else (upstream + "/v1/models")
            headers = {"Accept": "application/json", "User-Agent": "OpenGrok/1.0 (Mozilla/5.0)"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            try:
                req = urllib.request.Request(models_url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = []
                    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                        models = [m["id"] for m in data["data"] if isinstance(m, dict) and "id" in m]
                    elif isinstance(data, list):
                        models = [m.get("id", m) if isinstance(m, dict) else str(m) for m in data]
                    self._json(200, {"ok": True, "models": models})
            except urllib.error.HTTPError as e:
                err_text = e.read(500).decode("utf-8", "replace")
                self._json(200, {"ok": False, "error": f"HTTP {e.code}: {err_text[:120]}"})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/test":
            upstream = req_data.get("upstream", "").rstrip("/")
            model = req_data.get("model", "")
            api_key = req_data.get("apiKey", "") or os.environ.get("API_SERVER_KEY", "")
            if not api_key:
                key_file = (DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT) / ".api_key"
                if key_file.is_file():
                    try:
                        api_key = key_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass
            if not upstream or not model:
                self._json(400, {"ok": False, "error": "upstream and model required"})
                return

            probe_url = (upstream + "/chat/completions") if upstream.endswith("/v1") else (upstream + "/v1/chat/completions")
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            }).encode("utf-8")

            headers = {"Content-Type": "application/json", "User-Agent": "OpenGrok/1.0 (Mozilla/5.0)"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            try:
                req = urllib.request.Request(probe_url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    self._json(200, {"ok": True, "status": resp.getcode(), "message": "200 OK - 模型响应正常"})
            except urllib.error.HTTPError as e:
                err_text = e.read(500).decode("utf-8", "replace")
                self._json(200, {"ok": False, "error": f"HTTP {e.code}: {err_text[:120]}"})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/save-bindings":
            new_bindings = req_data.get("bindings")
            if not isinstance(new_bindings, dict):
                self._json(400, {"ok": False, "error": "bindings object required"})
                return
            data_dir = DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT
            data_dir.mkdir(parents=True, exist_ok=True)
            bindings_path = data_dir / "model-bindings.json"
            try:
                bindings_path.write_text(json.dumps(new_bindings, indent=2) + "\n", encoding="utf-8")
                # restart sand-host
                subprocess.run(["pkill", "-f", "host-main.cjs"], capture_output=True)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/save":
            upstream = req_data.get("upstream", "").rstrip("/")
            model = req_data.get("model", "")
            api_key = req_data.get("apiKey", "")
            effort = req_data.get("effort", "high")
            fast = bool(req_data.get("fast", False))
            if not upstream or not model:
                self._json(400, {"ok": False, "error": "upstream and model required"})
                return

            data_dir = DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT
            data_dir.mkdir(parents=True, exist_ok=True)
            key_file = data_dir / ".api_key"

            if api_key:
                os.environ["API_SERVER_KEY"] = api_key
                try:
                    key_file.write_text(api_key.strip(), encoding="utf-8")
                    key_file.chmod(0o600)
                except Exception:
                    pass
            elif key_file.is_file():
                try:
                    api_key = key_file.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
            if not api_key:
                api_key = os.environ.get("API_SERVER_KEY", "")

            # Auto-wrap if host-main.cjs not yet wrapped (bindings write below
            # runs AFTER, so the installer's default binding never wins).
            try:
                if DEFAULT_HOST.is_file():
                    content = DEFAULT_HOST.read_text(encoding="utf-8", errors="replace")
                    if "createProtoSession" in content and "opengrok-runtime" not in content:
                        subprocess.run([sys.executable, str(HERE / "install-stock-box.py"), "--skip-hop"], capture_output=True)
            except Exception:
                pass

            # Write model-bindings.json
            bindings_path = data_dir / "model-bindings.json"
            hop_base = "http://127.0.0.1:18790/v1"

            params = [
                {"id": "effort", "value": str(effort)},
                {"id": "thinking", "value": "true" if not fast else "false"},
            ]
            if fast:
                params.append({"id": "fast", "value": "true"})

            existing_bindings = get_bindings(data_dir)
            existing_agents = existing_bindings.get("agents") or {}
            prev_star = existing_agents.get("*") or {}
            existing_agents["*"] = {
                "name": model,
                "modelId": model,
                "provider": "custom",
                "hopBaseUrl": hop_base,
                "upstream": upstream,
                "parameters": params,
            }
            # Adaptive rules belong to the wildcard binding; a model swap must
            # not silently drop them.
            if prev_star.get("effortWhen"):
                existing_agents["*"]["effortWhen"] = prev_star["effortWhen"]

            bdoc = {
                "_comment": "configured via opengrok remote-dashboard",
                "agents": existing_agents
            }
            bindings_path.write_text(json.dumps(bdoc, indent=2) + "\n", encoding="utf-8")

            # Launch / restart hop-server if on box
            try:
                subprocess.run(["pkill", "-f", "hop-server.py"], capture_output=True)
                env = os.environ.copy()
                env["HERMES_HOP_UPSTREAM"] = upstream
                env["HERMES_HOP_PORT"] = "18790"
                env["HERMES_HOP_HOST"] = "127.0.0.1"
                if api_key:
                    env["API_SERVER_KEY"] = api_key
                subprocess.Popen([sys.executable, str(HERE / "hop-server.py")], env=env, start_new_session=True)
            except Exception:
                pass

            # Bounce sand-host so it takes effect
            subprocess.run(["pkill", "-f", "host-main.cjs"], capture_output=True)

            self._json(200, {"ok": True})
            return

        if self.path == "/api/restart-host":
            try:
                subprocess.run(["pkill", "-f", "host-main.cjs"], capture_output=True)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/install":
            try:
                r = subprocess.run(
                    [sys.executable, str(HERE / "install-stock-box.py"), "--skip-hop"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self._json(200, {"ok": r.returncode == 0, "output": r.stdout or r.stderr})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return

        self._json(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="Tailscale Web Control Panel for Grok Bot.")
    ap.add_argument("--port", type=int, default=8888, help="Port to listen on (default 8888)")
    ap.add_argument("--host", default="0.0.0.0", help="Host interface (default 0.0.0.0)")
    args = ap.parse_args()

    ensure_hop_server()
    if WATCHDOG_SEC > 0 and DEFAULT_HOST.parent.is_dir():
        threading.Thread(target=watchdog_loop, daemon=True).start()
    elif WATCHDOG_SEC > 0:
        print("[watchdog] not on a sandbox box (no %s) — watchdog off" % DEFAULT_HOST.parent, flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"⚡ OpenGrok Dashboard listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
        server.server_close()


if __name__ == "__main__":
    main()
