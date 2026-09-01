#!/usr/bin/env python3
"""remote-dashboard — Tailscale-friendly Web Control Panel for Grok Bot / OpenGrok.

Lets you remotely configure models, inspect live routing logs, test endpoints,
re-wrap Grok Bot host after updates, and restart sand-host over Tailscale.

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
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_DATA = Path("/home/box/sand-data")
DEFAULT_HOST = Path("/home/box/sand-host/host-main.cjs")
SESSION_LOG = Path("/tmp/opengrok-session.log")


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


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Grok Bot 远程控制中心 | OpenGrok Console</title>
  <style>
    :root {
      --bg: #0b0d13;
      --card: #131722;
      --card-hover: #191f2e;
      --border: #23293b;
      --accent: #6366f1;
      --accent-hover: #4f46e5;
      --text: #f3f4f6;
      --text-muted: #94a3b8;
      --success: #10b981;
      --warn: #f59e0b;
      --danger: #ef4444;
      --cyan: #06b6d4;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      min-height: 100vh;
      padding: 24px 16px;
      display: flex;
      justify-content: center;
    }
    .container { width: 100%; max-width: 980px; }
    
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 20px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
      gap: 12px;
    }
    .title h1 { font-size: 20px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px; }
    .title p { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
    
    .header-badges { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      background: #1e2433;
      color: var(--text-muted);
      border: 1px solid var(--border);
    }
    .badge.online { background: rgba(16, 185, 129, 0.15); color: var(--success); border-color: rgba(16, 185, 129, 0.35); }
    .badge.offline { background: rgba(239, 68, 68, 0.15); color: var(--danger); border-color: rgba(239, 68, 68, 0.35); }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }

    /* Overview Status Cards */
    .status-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    .stat-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .stat-card .label { font-size: 12px; color: var(--text-muted); font-weight: 500; }
    .stat-card .val { font-size: 14px; font-weight: 700; font-family: ui-monospace, monospace; color: #fff; word-break: break-all; }

    /* Tabs Navigation */
    .tabs {
      display: flex;
      gap: 8px;
      margin-bottom: 16px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
      overflow-x: auto;
    }
    .tab-btn {
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 8px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s;
    }
    .tab-btn:hover { background: #1a202c; color: var(--text); }
    .tab-btn.active { background: var(--accent); color: #fff; }

    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* Main Card Form */
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
    }
    .card h2 { font-size: 15px; font-weight: 600; margin-bottom: 14px; color: #fff; display: flex; justify-content: space-between; align-items: center; }
    
    .form-group { margin-bottom: 16px; }
    label { display: block; font-size: 12px; font-weight: 600; color: var(--text-muted); margin-bottom: 6px; }
    input, select, textarea {
      width: 100%;
      background: #090b10;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--text);
      font-size: 13px;
      outline: none;
      transition: border-color 0.15s;
      font-family: inherit;
    }
    input:focus, select:focus, textarea:focus { border-color: var(--accent); }
    
    .btn-group { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    button {
      background: var(--accent);
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 9px 16px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: filter 0.15s, transform 0.05s;
    }
    button:hover { filter: brightness(1.15); }
    button:active { transform: scale(0.98); }
    button.secondary { background: #1e2433; color: var(--text); border: 1px solid var(--border); }
    button.danger { background: #7f1d1d; color: #fca5a5; }
    button.success { background: #065f46; color: #a7f3d0; }

    .pill {
      font-size: 11px;
      padding: 4px 8px;
      border-radius: 6px;
      background: #1a202c;
      color: #93c5fd;
      cursor: pointer;
      user-select: none;
      border: 1px solid rgba(147, 197, 253, 0.15);
      transition: all 0.15s;
    }
    .pill:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
    .quick-picks { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
    
    #msgBox { margin-bottom: 16px; padding: 10px 14px; border-radius: 8px; font-size: 13px; display: none; }
    #msgBox.ok { background: rgba(16, 185, 129, 0.15); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }
    #msgBox.err { background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }

    /* Tables */
    table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
    th { text-align: left; padding: 10px; color: var(--text-muted); border-bottom: 1px solid var(--border); font-weight: 600; }
    td { padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.03); vertical-align: middle; }
    tr:hover td { background: rgba(255,255,255,0.02); }

    .log-box {
      background: #07080c;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
      color: #cbd5e1;
      height: 320px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
      line-height: 1.5;
    }
    .log-highlight-route { color: #38bdf8; font-weight: bold; }
    .log-highlight-error { color: #f87171; font-weight: bold; }
    .log-highlight-tool { color: #34d399; }
    .log-highlight-effort { color: #fbbf24; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title">
        <h1><span>⚡</span> Grok Bot 远程控制中心</h1>
        <p>支持 xAI Grok 原生透传、多 Agent 独立路由与深度推理控制</p>
      </div>
      <div class="header-badges">
        <div id="hostBadge" class="badge">
          <span class="dot"></span> <span id="hostStatusText">检测中...</span>
        </div>
        <div id="hopBadge" class="badge">
          <span class="dot"></span> <span id="hopStatusText">Hop 代理</span>
        </div>
      </div>
    </header>

    <div id="msgBox"></div>

    <!-- Status Overview Bar -->
    <div class="status-grid">
      <div class="stat-card">
        <span class="label">Tailscale 安全 IP</span>
        <span id="statIp" class="val">-</span>
      </div>
      <div class="stat-card">
        <span class="label">当前活跃模型 / Route</span>
        <span id="statModel" class="val" style="color:var(--cyan);">-</span>
      </div>
      <div class="stat-card">
        <span class="label">推理深度 & 思考开关</span>
        <span id="statEffort" class="val" style="color:var(--warn);">-</span>
      </div>
      <div class="stat-card">
        <span class="label">上游服务根地址</span>
        <span id="statUpstream" class="val">-</span>
      </div>
      <div class="stat-card">
        <span class="label">API Key 凭据状态</span>
        <span id="statApiKey" class="val">-</span>
      </div>
    </div>

    <!-- Navigation Tabs -->
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('primary')">🌟 主力模型与预设</button>
      <button class="tab-btn" onclick="switchTab('agents')">🤖 多 Agent 路由分发</button>
      <button class="tab-btn" onclick="switchTab('adaptive')">⚡ 自适应深度 (effortWhen)</button>
      <button class="tab-btn" onclick="switchTab('system')">📊 系统诊断与运维</button>
      <button class="tab-btn" onclick="switchTab('logs')">📜 实时会话日志</button>
    </div>

    <!-- TAB 1: Primary Model -->
    <div id="tab-primary" class="tab-content active">
      <div class="card">
        <h2>主流供应商一键预设 <span style="font-size:12px; color:var(--text-muted); font-weight:normal;">(点击快速填充 | 🌟 = 渠道默认 high 推理)</span></h2>
        <div class="quick-picks">
          <span class="pill" style="border-color:#10b981; color:#6ee7b7; font-weight:bold;" onclick="useCurrentConfig()">✨ 我的自有渠道 (读取沙箱当前配置) 🌟</span>
          <span class="pill" style="border-color:#6366f1; color:#a5b4fc;" onclick="setPreset('xai-grok4')">⚡ xAI Grok-4.6 🌟</span>
          <span class="pill" style="border-color:#6366f1; color:#a5b4fc;" onclick="setPreset('xai-grok2')">⚡ xAI Grok-2 🌟</span>
          <span class="pill" onclick="setPreset('deepseek-r1')">🧠 DeepSeek-R1 🌟</span>
          <span class="pill" onclick="setPreset('deepseek-v3')">🚀 DeepSeek-V3 🌟</span>
          <span class="pill" onclick="setPreset('claude-37')">🔮 Claude 3.7 🌟</span>
          <span class="pill" onclick="setPreset('gemini-25')">💎 Gemini 2.5 🌟</span>
          <span class="pill" onclick="setPreset('glm-flash')">🇨🇳 智谱 GLM 🌟</span>
          <span class="pill" onclick="setPreset('openai-4o')">🟢 OpenAI GPT-4o 🌟</span>
          <span class="pill" onclick="setPreset('ollama')">💻 本地 Ollama</span>
        </div>

        <div class="form-group">
          <label>上游 API 根地址 (Upstream Base URL)</label>
          <input type="text" id="upstreamUrl" placeholder="https://api.x.ai/v1 或 https://api.deepseek.com" autocomplete="off">
        </div>

        <div class="form-group">
          <label>API Key / 鉴权令牌 (存储在沙箱 .api_key 中，保密注入)</label>
          <input type="password" id="apiKey" placeholder="留空则保持现有 Key 不变 (例如 xai-... / sk-...)" autocomplete="new-password">
        </div>

        <div class="form-group">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <label style="margin:0;">模型标识符 (Model Slug)</label>
            <button class="secondary" style="padding:3px 8px; font-size:11px;" onclick="fetchUpstreamModels()">🔍 自动获取上游可用模型</button>
          </div>
          <input type="text" id="modelSlug" placeholder="grok-4.6 / deepseek-reasoner / claude-3-7-sonnet" list="modelList">
          <datalist id="modelList"></datalist>
          <div id="modelsBadges" style="margin-top:8px; display:flex; gap:4px; flex-wrap:wrap;"></div>
        </div>

        <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px;">
          <div class="form-group">
            <label>推理深度等级 (Reasoning Effort)</label>
            <select id="effortSelect">
              <option value="high">high (推荐，深度思考)</option>
              <option value="xhigh">xhigh / max (满血最强思考)</option>
              <option value="medium">medium (中度思考)</option>
              <option value="low">low (轻量思考)</option>
            </select>
          </div>

          <div class="form-group" style="display:flex; flex-direction:column; justify-content:center;">
            <label style="cursor:pointer; display:flex; align-items:center; gap:8px; margin-top:14px;">
              <input type="checkbox" id="fastToggle" style="accent-color:var(--accent); width:16px; height:16px; cursor:pointer;">
              <span>开启极速响应模式 (Fast Lane / 关闭多余思考)</span>
            </label>
          </div>
        </div>

        <div class="btn-group" style="justify-content: flex-end;">
          <button class="secondary" onclick="testConnection()">🧪 测试模型连通性</button>
          <button onclick="saveAndApply()">💾 保存配置并应用到沙箱</button>
        </div>
      </div>
    </div>

    <!-- TAB 2: Multi-Agent Routing -->
    <div id="tab-agents" class="tab-content">
      <div class="card">
        <h2>多 Agent 路由规则清单 <span class="pill" onclick="openAddAgentModal()">+ 新增 Agent 绑定</span></h2>
        <p style="font-size:12px; color:var(--text-muted); margin-bottom:12px;">
          Grok Bot 可以在不同 Agent / 对话中提取唯一 UUID。未命中的 Agent 将统一回退至全局默认模型 (<code>*</code>)。
        </p>
        <table id="agentsTable">
          <thead>
            <tr>
              <th>Agent UUID / 标识</th>
              <th>别名描述</th>
              <th>绑定模型</th>
              <th>推理配置</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="agentsTableBody">
            <tr><td colspan="5" style="text-align:center; color:var(--text-muted);">正在加载 Agent 规则...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- TAB 3: Adaptive Effort (effortWhen) -->
    <div id="tab-adaptive" class="tab-content">
      <div class="card">
        <h2>上下文自适应推理深度 (effortWhen)</h2>
        <p style="font-size:12px; color:var(--text-muted); margin-bottom:16px;">
          当上下文最近消息中包含以下关键词时，系统将动态调整该回合的推理深度（例如遇到架构重构时启用 high/max，遇到查看状态/diff 时降低为 medium/low）。
        </p>

        <div class="form-group">
          <label>触发 Medium 思考的关键词 (用英文逗号分隔)</label>
          <input type="text" id="effortWhenMedium" placeholder="例如: git diff, status, test, review">
        </div>

        <div class="form-group">
          <label>触发 High / Max 深度思考的关键词 (用英文逗号分隔)</label>
          <input type="text" id="effortWhenHigh" placeholder="例如: refactor, architect, audit, security, math, bugfix">
        </div>

        <div class="btn-group" style="justify-content: flex-end;">
          <button onclick="saveAdaptiveRules()">💾 保存自适应深度规则</button>
        </div>
      </div>
    </div>

    <!-- TAB 4: System Diagnostics -->
    <div id="tab-system" class="tab-content">
      <div class="card">
        <h2>沙箱运维与控制</h2>
        <div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px;">
          <button class="secondary" onclick="restartSandHost()">🔄 热重启 Grok sand-host</button>
          <button class="secondary" onclick="reinstallHost()">🛠️ 重新安装/注入 OpenGrok 补丁</button>
          <button class="secondary" onclick="runDoctor()">🩺 运行环境 Doctor 诊断</button>
          <button class="secondary" onclick="refreshStatus(true)">⚡ 刷新同步状态</button>
        </div>
        <div id="doctorOutputBox" style="display:none;" class="form-group">
          <label>诊断输出结果</label>
          <div id="doctorOutput" class="log-box" style="height:180px;"></div>
        </div>
      </div>
    </div>

    <!-- TAB 5: Live Logs -->
    <div id="tab-logs" class="tab-content">
      <div class="card">
        <h2>
          <span>会话与路由实时日志 (/tmp/opengrok-session.log)</span>
          <div style="display:flex; gap:8px;">
            <button class="secondary" style="padding:4px 8px; font-size:11px;" onclick="clearLogs()">🗑️ 清空日志</button>
            <button class="secondary" style="padding:4px 8px; font-size:11px;" onclick="fetchLogs()">↻ 刷新</button>
          </div>
        </h2>
        <div style="margin-bottom:10px; display:flex; gap:10px;">
          <input type="text" id="logFilter" placeholder="🔍 过滤日志关键词 (例如 route / SendToUser / error / reasoning)..." oninput="applyLogFilter()">
          <label style="display:flex; align-items:center; gap:4px; font-size:12px; cursor:pointer; white-space:nowrap;">
            <input type="checkbox" id="autoScrollLog" checked> 自动滚到底部
          </label>
        </div>
        <div id="logBox" class="log-box">正在载入实时日志...</div>
      </div>
    </div>
  </div>

  <!-- Agent Edit/Add Modal -->
  <div id="agentModal" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.7); z-index:99; justify-content:center; align-items:center; padding:16px;">
    <div class="card" style="width:100%; max-width:540px; margin:0;">
      <h2 id="agentModalTitle">新增 Agent 绑定规则</h2>
      <div class="form-group">
        <label>Agent UUID 标识 (或输入 * 代表全局)</label>
        <input type="text" id="modalAgentId" placeholder="例如: 00000000-0000-4000-8000-000000000001">
      </div>
      <div class="form-group">
        <label>Agent 别名描述</label>
        <input type="text" id="modalAgentName" placeholder="例如: 架构评审专家 / 代码审查员">
      </div>
      <div class="form-group">
        <label>绑定模型 Slug</label>
        <input type="text" id="modalAgentModel" placeholder="例如: grok-4.6 / claude-3-7-sonnet">
      </div>
      <div class="form-group">
        <label>推理深度</label>
        <select id="modalAgentEffort">
          <option value="high">high (深度思考)</option>
          <option value="xhigh">xhigh / max (极致思考)</option>
          <option value="medium">medium (中度思考)</option>
          <option value="low">low (轻量思考)</option>
        </select>
      </div>
      <div class="btn-group" style="justify-content:flex-end;">
        <button class="secondary" onclick="closeAgentModal()">取消</button>
        <button onclick="saveAgentFromModal()">确认保存</button>
      </div>
    </div>
  </div>

  <script>
    let globalBindings = { agents: {} };
    let rawLogText = "";

    const PRESETS = {
      'xai-grok4': { url: 'https://api.x.ai/v1', model: 'grok-4.6', effort: 'high', desc: '⚡ xAI Grok-4.6 (官方)' },
      'xai-grok2': { url: 'https://api.x.ai/v1', model: 'grok-2-latest', effort: 'high', desc: '⚡ xAI Grok-2-Latest' },
      'deepseek-r1': { url: 'https://api.deepseek.com', model: 'deepseek-reasoner', effort: 'high', desc: '🧠 DeepSeek-R1 (推理)' },
      'deepseek-v3': { url: 'https://api.deepseek.com', model: 'deepseek-chat', effort: 'high', desc: '🚀 DeepSeek-V3 (通用)' },
      'claude-37': { url: 'https://openrouter.ai/api', model: 'anthropic/claude-3.7-sonnet', effort: 'high', desc: '🔮 Claude 3.7 Sonnet' },
      'gemini-25': { url: 'https://generativelanguage.googleapis.com', model: 'gemini-2.5-pro', effort: 'high', desc: '💎 Gemini 2.5 Pro' },
      'glm-flash': { url: 'https://open.bigmodel.cn/api/paas', model: 'glm-5.3-flash', effort: 'high', desc: '🇨🇳 智谱 GLM-5.3' },
      'openai-4o': { url: 'https://api.openai.com', model: 'gpt-4o', effort: 'high', desc: '🟢 OpenAI GPT-4o' },
      'ollama': { url: 'http://127.0.0.1:11434', model: 'qwen2.5-coder', effort: 'low', desc: '💻 本地 Ollama' },
    };

    function switchTab(name) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      event.target.classList.add('active');
      const target = document.getElementById('tab-' + name);
      if (target) target.classList.add('active');
      if (name === 'logs') fetchLogs();
    }

    function setPreset(key) {
      const p = PRESETS[key];
      if (p) {
        document.getElementById('upstreamUrl').value = p.url;
        document.getElementById('modelSlug').value = p.model;
        document.getElementById('effortSelect').value = p.effort || 'high';
        document.getElementById('fastToggle').checked = false;
        showMsg(`✅ 已填充: ${p.desc || p.model} (推理深度: ${p.effort || 'high'})`, true);
      }
    }

    // 自有渠道不写死在代码里：直接回读沙箱当前生效的配置，避免私有端点进入仓库
    function useCurrentConfig() {
      const a = (globalBindings.agents && (globalBindings.agents['*'] || Object.values(globalBindings.agents)[0])) || {};
      if (!a.upstream && !a.modelId) {
        showMsg('❌ 沙箱当前没有已保存的渠道配置，请先手动填写并保存一次', false);
        return;
      }
      if (a.upstream) document.getElementById('upstreamUrl').value = a.upstream;
      if (a.modelId) document.getElementById('modelSlug').value = a.modelId;
      const eff = (a.parameters || []).find(p => p.id === 'effort');
      document.getElementById('effortSelect').value = eff ? eff.value : 'high';
      document.getElementById('fastToggle').checked = false;
      showMsg(`✅ 已载入沙箱当前配置: ${a.modelId || '-'} (推理深度: ${eff ? eff.value : 'high'})`, true);
    }

    function selectModel(name) {
      document.getElementById('modelSlug').value = name;
    }

    function showMsg(text, isOk) {
      const box = document.getElementById('msgBox');
      box.textContent = text;
      box.className = isOk ? 'ok' : 'err';
      box.style.display = 'block';
      setTimeout(() => { box.style.display = 'none'; }, 6000);
    }

    async function fetchUpstreamModels() {
      const upstream = document.getElementById('upstreamUrl').value.trim();
      const key = document.getElementById('apiKey').value.trim();
      if (!upstream) {
        alert('请先输入上游 API 根地址');
        return;
      }
      showMsg('正在获取上游模型列表...', true);
      try {
        const res = await fetch('/api/models', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ upstream, apiKey: key })
        });
        const d = await res.json();
        if (d.ok && Array.isArray(d.models) && d.models.length > 0) {
          const list = document.getElementById('modelList');
          list.innerHTML = '';
          const badges = document.getElementById('modelsBadges');
          badges.innerHTML = '';
          d.models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m;
            list.appendChild(opt);

            const badge = document.createElement('span');
            badge.className = 'pill';
            badge.textContent = m;
            badge.onclick = () => selectModel(m);
            badges.appendChild(badge);
          });
          showMsg(`✅ 成功获取 ${d.models.length} 个可用模型 (点击可快速选择)`, true);
        } else {
          showMsg('❌ 获取失败: ' + (d.error || '未返回可用模型'), false);
        }
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    async function testConnection() {
      const upstream = document.getElementById('upstreamUrl').value.trim();
      const model = document.getElementById('modelSlug').value.trim();
      const key = document.getElementById('apiKey').value.trim();
      if (!upstream || !model) {
        alert('请先填写上游地址和模型名称');
        return;
      }
      showMsg('正在向端点发送测试探针...', true);
      try {
        const res = await fetch('/api/test', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ upstream, model, apiKey: key })
        });
        const d = await res.json();
        if (d.ok) {
          showMsg('✅ 探测成功: ' + (d.message || '模型已就绪并可正常响应'), true);
        } else {
          showMsg('❌ 探测失败: ' + (d.error || '无法连接'), false);
        }
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    async function saveAndApply() {
      const upstream = document.getElementById('upstreamUrl').value.trim();
      const model = document.getElementById('modelSlug').value.trim();
      const key = document.getElementById('apiKey').value.trim();
      const effort = document.getElementById('effortSelect').value;
      const fast = document.getElementById('fastToggle').checked;

      if (!upstream || !model) {
        alert('请完整填写上游根地址和模型标识');
        return;
      }

      showMsg('正在保存并热重启沙箱服务...', true);
      try {
        const res = await fetch('/api/save', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ upstream, model, apiKey: key, effort, fast })
        });
        const d = await res.json();
        if (d.ok) {
          showMsg('✅ 配置已更新并应用！Grok 宿主进程已重启。', true);
          document.getElementById('apiKey').value = '';
          setTimeout(() => refreshStatus(false), 1500);
        } else {
          showMsg('❌ 保存失败: ' + (d.error || '未知错误'), false);
        }
      } catch (e) {
        showMsg('❌ 保存异常: ' + e.message, false);
      }
    }

    function renderAgentsTable(agents) {
      const tbody = document.getElementById('agentsTableBody');
      tbody.innerHTML = '';
      const keys = Object.keys(agents || {});
      if (!keys.length) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted);">暂无配置的 Agent 规则 (全部使用默认模型)</td></tr>';
        return;
      }
      keys.forEach(k => {
        const a = agents[k] || {};
        const tr = document.createElement('tr');
        const params = (a.parameters || []).map(p => `${p.id}=${p.value}`).join(', ') || '默认';
        tr.innerHTML = `
          <td><code>${k === '*' ? '★ 全局默认 (*)' : k}</code></td>
          <td>${a.name || '-'}</td>
          <td><b style="color:var(--cyan);">${a.modelId || '-'}</b></td>
          <td><span style="font-size:12px; color:var(--text-muted);">${params}</span></td>
          <td>
            <button class="secondary" style="padding:2px 8px; font-size:11px;" onclick="editAgent('${k}')">编辑</button>
            ${k !== '*' ? `<button class="danger" style="padding:2px 8px; font-size:11px;" onclick="deleteAgent('${k}')">删除</button>` : ''}
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    function openAddAgentModal() {
      document.getElementById('agentModalTitle').textContent = '新增 Agent 路由绑定';
      document.getElementById('modalAgentId').value = '';
      document.getElementById('modalAgentName').value = '';
      document.getElementById('modalAgentModel').value = '';
      document.getElementById('modalAgentEffort').value = 'high';
      document.getElementById('agentModal').style.display = 'flex';
    }

    function closeAgentModal() {
      document.getElementById('agentModal').style.display = 'none';
    }

    function editAgent(key) {
      const a = (globalBindings.agents && globalBindings.agents[key]) || {};
      document.getElementById('agentModalTitle').textContent = '编辑 Agent 绑定: ' + key;
      document.getElementById('modalAgentId').value = key;
      document.getElementById('modalAgentName').value = a.name || '';
      document.getElementById('modalAgentModel').value = a.modelId || '';
      const eff = (a.parameters || []).find(p => p.id === 'effort');
      document.getElementById('modalAgentEffort').value = eff ? eff.value : 'high';
      document.getElementById('agentModal').style.display = 'flex';
    }

    async function deleteAgent(key) {
      if (!confirm('确定要删除 Agent 绑定 [' + key + '] 吗？')) return;
      delete globalBindings.agents[key];
      await syncFullBindings();
    }

    async function saveAgentFromModal() {
      const key = document.getElementById('modalAgentId').value.trim();
      const name = document.getElementById('modalAgentName').value.trim();
      const model = document.getElementById('modalAgentModel').value.trim();
      const effort = document.getElementById('modalAgentEffort').value;
      if (!key || !model) {
        alert('请填写 Agent 标识与模型名称');
        return;
      }
      if (!globalBindings.agents) globalBindings.agents = {};
      const baseUpstream = document.getElementById('upstreamUrl').value.trim() || 'https://api.x.ai/v1';
      globalBindings.agents[key] = {
        name: name || model,
        modelId: model,
        provider: 'custom',
        hopBaseUrl: 'http://127.0.0.1:18790/v1',
        upstream: baseUpstream,
        parameters: [
          { id: 'effort', value: effort },
          { id: 'thinking', value: 'true' }
        ]
      };
      closeAgentModal();
      await syncFullBindings();
    }

    async function syncFullBindings() {
      showMsg('正在同步 Agent 路由配置...', true);
      try {
        const res = await fetch('/api/save-bindings', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ bindings: globalBindings })
        });
        const d = await res.json();
        if (d.ok) {
          showMsg('✅ Agent 路由配置已更新！', true);
          refreshStatus(false);
        } else {
          showMsg('❌ 保存失败: ' + (d.error || '未知错误'), false);
        }
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    async function saveAdaptiveRules() {
      const med = document.getElementById('effortWhenMedium').value.split(',').map(s => s.trim()).filter(Boolean);
      const hi = document.getElementById('effortWhenHigh').value.split(',').map(s => s.trim()).filter(Boolean);
      const agent = (globalBindings.agents && (globalBindings.agents['*'] || Object.values(globalBindings.agents)[0])) || null;
      if (agent) {
        agent.effortWhen = {};
        if (med.length) agent.effortWhen.medium = med;
        if (hi.length) agent.effortWhen.high = hi;
      }
      await syncFullBindings();
    }

    async function refreshStatus(forceSyncForm = false) {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        
        document.getElementById('statIp').textContent = data.tailscale_ip || '127.0.0.1';
        
        // Host Badge
        const hostBadge = document.getElementById('hostBadge');
        const hostStatusText = document.getElementById('hostStatusText');
        if (data.sand_host && data.sand_host.running) {
          hostBadge.className = 'badge online';
          hostStatusText.textContent = 'sand-host PID ' + data.sand_host.pids.join(', ');
        } else {
          hostBadge.className = 'badge offline';
          hostStatusText.textContent = 'sand-host 未运行';
        }

        // Hop Badge
        const hopBadge = document.getElementById('hopBadge');
        const hopStatusText = document.getElementById('hopStatusText');
        if (data.hop_server && data.hop_server.running) {
          hopBadge.className = 'badge online';
          hopStatusText.textContent = 'Hop 代理 PID ' + data.hop_server.pids.join(', ');
        } else {
          hopBadge.className = 'badge offline';
          hopStatusText.textContent = 'Hop 代理未启动 ⚠️';
        }

        globalBindings = data.bindings || { agents: {} };
        const agent = (globalBindings.agents && (globalBindings.agents['*'] || Object.values(globalBindings.agents)[0])) || {};

        document.getElementById('statModel').textContent = agent.modelId || '未配置';
        
        const effortParam = (agent.parameters || []).find(p => p.id === 'effort');
        const thinkingParam = (agent.parameters || []).find(p => p.id === 'thinking');
        const fastParam = (agent.parameters || []).find(p => p.id === 'fast');
        let effortText = effortParam ? `effort=${effortParam.value}` : 'default';
        if (thinkingParam) effortText += ` thinking=${thinkingParam.value}`;
        if (fastParam && (fastParam.value === 'true' || fastParam.value === true)) effortText += ' fast=true';
        document.getElementById('statEffort').textContent = effortText;
        
        document.getElementById('statUpstream').textContent = agent.upstream || 'http://127.0.0.1:18790';
        document.getElementById('statApiKey').textContent = (data.api_key_info && data.api_key_info.configured)
          ? `已配置 (${data.api_key_info.preview})`
          : '未设置 ⚠️';

        renderAgentsTable(globalBindings.agents);

        // Fill form on initial load
        if (forceSyncForm || !document.getElementById('upstreamUrl').value) {
          if (agent.upstream) document.getElementById('upstreamUrl').value = agent.upstream;
          if (agent.modelId) document.getElementById('modelSlug').value = agent.modelId;
          const eff = (agent.parameters || []).find(p => p.id === 'effort');
          if (eff) document.getElementById('effortSelect').value = eff.value;
          const fast = (agent.parameters || []).find(p => p.id === 'fast');
          document.getElementById('fastToggle').checked = !!(fast && (fast.value === true || fast.value === 'true'));
          
          if (agent.effortWhen) {
            document.getElementById('effortWhenMedium').value = (agent.effortWhen.medium || []).join(', ');
            document.getElementById('effortWhenHigh').value = (agent.effortWhen.high || []).join(', ');
          }
        }

        if (data.session_log) {
          rawLogText = data.session_log;
          applyLogFilter();
        }
      } catch (e) {
        console.error('refresh error:', e);
      }
    }

    function applyLogFilter() {
      const filter = document.getElementById('logFilter').value.toLowerCase().trim();
      const logBox = document.getElementById('logBox');
      const lines = rawLogText.split('\n');
      const filtered = filter ? lines.filter(l => l.toLowerCase().includes(filter)) : lines;
      logBox.innerHTML = filtered.map(line => {
        let safe = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        if (safe.includes('route ')) safe = `<span class="log-highlight-route">${safe}</span>`;
        else if (safe.includes('error') || safe.includes('Error')) safe = `<span class="log-highlight-error">${safe}</span>`;
        else if (safe.includes('tool_call') || safe.includes('SendToUser')) safe = `<span class="log-highlight-tool">${safe}</span>`;
        else if (safe.includes('effortWhen') || safe.includes('params=')) safe = `<span class="log-highlight-effort">${safe}</span>`;
        return safe;
      }).join('\n') || '(无匹配日志内容)';

      if (document.getElementById('autoScrollLog').checked) {
        logBox.scrollTop = logBox.scrollHeight;
      }
    }

    async function fetchLogs() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        if (data.session_log) {
          rawLogText = data.session_log;
          applyLogFilter();
        }
      } catch (e) {}
    }

    async function clearLogs() {
      if (!confirm('确定清空会话日志吗？')) return;
      try {
        await fetch('/api/clear-log', { method: 'POST' });
        rawLogText = '';
        applyLogFilter();
        showMsg('✅ 日志已清空', true);
      } catch (e) {}
    }

    async function restartSandHost() {
      showMsg('正在热重启 Grok sand-host...', true);
      try {
        const res = await fetch('/api/restart-host', { method: 'POST' });
        const d = await res.json();
        if (d.ok) {
          showMsg('✅ sand-host 已重启生效', true);
          setTimeout(() => refreshStatus(false), 2000);
        } else {
          showMsg('❌ 重启失败: ' + d.error, false);
        }
      } catch (e) {
        showMsg('❌ 重启异常: ' + e.message, false);
      }
    }

    async function reinstallHost() {
      showMsg('正在重新安装/注入 OpenGrok 补丁...', true);
      try {
        const res = await fetch('/api/install', { method: 'POST' });
        const d = await res.json();
        if (d.ok) {
          showMsg('✅ 补丁已重新注入！', true);
          setTimeout(() => refreshStatus(false), 2000);
        } else {
          showMsg('❌ 注入失败: ' + (d.error || d.output), false);
        }
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    async function runDoctor() {
      showMsg('正在运行 Doctor 诊断...', true);
      try {
        const res = await fetch('/api/doctor');
        const d = await res.json();
        document.getElementById('doctorOutputBox').style.display = 'block';
        document.getElementById('doctorOutput').textContent = d.output || '诊断完成';
        showMsg(d.ok ? '✅ 诊断通过' : '⚠️ 诊断发现异常，请查看下方输出', d.ok);
      } catch (e) {
        showMsg('❌ 诊断异常: ' + e.message, false);
      }
    }

    // Auto-refresh on interval
    refreshStatus(true);
    setInterval(() => refreshStatus(false), 6000);
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
            ts_ip = get_tailscale_ip()
            sand_status = get_sand_host_status()
            hop_status = get_hop_server_status()
            data_dir = DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT
            bindings = get_bindings(data_dir)
            api_key_info = get_api_key_info(data_dir)
            session_log = read_last_lines(SESSION_LOG, 120)
            wrapped = False
            if DEFAULT_HOST.is_file():
                try:
                    wrapped = "opengrok-runtime" in DEFAULT_HOST.read_text(encoding="utf-8")
                except Exception:
                    pass

            self._json(200, {
                "tailscale_ip": ts_ip,
                "sand_host": sand_status,
                "hop_server": hop_status,
                "api_key_info": api_key_info,
                "bindings": bindings,
                "wrapped": wrapped,
                "session_log": session_log,
            })
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

            # Auto-wrap if host-main.cjs not yet wrapped
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
            existing_agents["*"] = {
                "name": model,
                "modelId": model,
                "provider": "custom",
                "hopBaseUrl": hop_base,
                "upstream": upstream,
                "parameters": params,
            }

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

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"⚡ OpenGrok Dashboard listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
        server.server_close()


if __name__ == "__main__":
    main()
