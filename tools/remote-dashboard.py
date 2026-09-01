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


def read_last_lines(path: Path, max_lines: int = 80) -> str:
    if not path.is_file():
        return ""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
        lines = txt.strip().splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception as e:
        return f"Error reading log: {e}"


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Grok Bot 远程控制面板 | OpenGrok</title>
  <style>
    :root {
      --bg: #090a0f;
      --card: #12141c;
      --card-hover: #181b26;
      --border: #222634;
      --accent: #6366f1;
      --accent-hover: #4f46e5;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --success: #10b981;
      --warn: #f59e0b;
      --danger: #ef4444;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      min-height: 100vh;
      padding: 24px 16px;
      display: flex;
      justify-content: center;
    }
    .container { width: 100%; max-width: 860px; }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    .title h1 { font-size: 20px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px; }
    .title p { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      background: #1f2937;
      color: var(--text-muted);
    }
    .badge.online { background: rgba(16, 185, 129, 0.15); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }
    .badge.offline { background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
    
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
    @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
    
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
    }
    .card h2 { font-size: 15px; font-weight: 600; margin-bottom: 14px; color: #fff; display: flex; justify-content: space-between; }
    
    .status-row { display: flex; justify-content: space-between; font-size: 13px; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
    .status-row:last-child { border-bottom: none; }
    .status-label { color: var(--text-muted); }
    .status-val { font-family: monospace; font-weight: 600; }
    
    .form-group { margin-bottom: 14px; }
    label { display: block; font-size: 12px; font-weight: 600; color: var(--text-muted); margin-bottom: 6px; }
    input, select {
      width: 100%;
      background: #0d0f16;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px 12px;
      color: var(--text);
      font-size: 13px;
      outline: none;
      transition: border-color 0.15s;
    }
    input:focus, select:focus { border-color: var(--accent); }
    
    .btn-group { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
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
    button.secondary { background: #242938; color: var(--text); }
    button.danger { background: #7f1d1d; color: #fca5a5; }
    
    .log-box {
      background: #090a0f;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
      color: #cbd5e1;
      height: 220px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .pill {
      font-size: 11px;
      padding: 2px 6px;
      border-radius: 4px;
      background: #1e2433;
      color: #93c5fd;
      cursor: pointer;
      user-select: none;
    }
    .pill:hover { background: var(--accent); color: #fff; }
    .quick-picks { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
    #msgBox { margin-top: 12px; padding: 8px 12px; border-radius: 6px; font-size: 12px; display: none; }
    #msgBox.ok { background: rgba(16, 185, 129, 0.15); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }
    #msgBox.err { background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title">
        <h1><span>⚡</span> Grok Bot 远程管理面板</h1>
        <p>基于 Tailscale 安全内网的自定义模型与沙箱控制台</p>
      </div>
      <div id="hostBadge" class="badge">
        <span class="dot"></span> <span id="hostStatusText">检测中...</span>
      </div>
    </header>

    <div class="grid">
      <!-- 状态卡片 -->
      <div class="card">
        <h2>沙箱环境状态 <span class="pill" onclick="refreshStatus()">刷新 ↻</span></h2>
        <div class="status-row">
          <span class="status-label">Tailscale IP</span>
          <span id="tsIp" class="status-val">-</span>
        </div>
        <div class="status-row">
          <span class="status-label">Grok sand-host 进程</span>
          <span id="sandHostPid" class="status-val">-</span>
        </div>
        <div class="status-row">
          <span class="status-label">当前活跃模型</span>
          <span id="curModel" class="status-val">-</span>
        </div>
        <div class="status-row">
          <span class="status-label">Hop 代理地址</span>
          <span id="curHop" class="status-val">-</span>
        </div>
        <div class="status-row">
          <span class="status-label">原生注入状态</span>
          <span id="curWrapStatus" class="status-val">-</span>
        </div>
      </div>

      <!-- 快速运维操作 -->
      <div class="card">
        <h2>快速运维控制</h2>
        <p style="font-size:12px; color:var(--text-muted); margin-bottom:14px;">
          Grok Bot 官方更新后，或修改配置后执行热加载：
        </p>
        <div class="btn-group">
          <button onclick="actionRestartHost()">🔄 重启 sand-host</button>
          <button class="secondary" onclick="actionReinstallWrap()">⚡ 重新注入 Host 包装</button>
          <button class="secondary" onclick="actionRunDoctor()">🩺 运行 Doctor 诊断</button>
        </div>
        <div id="msgBox"></div>
      </div>
    </div>

    <!-- 模型配置表单 -->
    <div class="card" style="margin-bottom: 20px;">
      <h2>配置并切换模型</h2>
      <div class="quick-picks">
        <span style="font-size:12px; color:var(--text-muted); line-height:22px;">快捷预设:</span>
        <span class="pill" onclick="setPreset('deepseek')">DeepSeek V3</span>
        <span class="pill" onclick="setPreset('deepseek-r1')">DeepSeek R1</span>
        <span class="pill" onclick="setPreset('glm')">智谱 GLM-5.3</span>
        <span class="pill" onclick="setPreset('openai')">OpenAI GPT-4o</span>
        <span class="pill" onclick="setPreset('claude')">Claude 3.7 (OpenRouter)</span>
        <span class="pill" onclick="setPreset('ollama')">本地 Ollama / vLLM</span>
      </div>

      <div class="form-group">
        <label>上游 API 根地址 (Upstream Base URL, 无需加 /v1)</label>
        <input type="text" id="upstreamUrl" placeholder="例如: https://api.deepseek.com 或 https://api.deepseek.com">
      </div>
      <div class="grid" style="margin-bottom: 0;">
        <div class="form-group">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <label style="margin-bottom:0;">模型名称 (Model Slug)</label>
            <a href="javascript:void(0)" onclick="fetchUpstreamModels()" style="font-size:12px; color:var(--accent); text-decoration:none;">📋 获取上游可用模型</a>
          </div>
          <input type="text" id="modelSlug" list="modelList" placeholder="例如: claude-sonnet-4-6 或 deepseek-chat" style="margin-top:6px;">
          <datalist id="modelList"></datalist>
          <div id="modelsBadges" style="margin-top:6px; display:flex; flex-wrap:wrap; gap:4px; max-height:80px; overflow-y:auto;"></div>
        </div>
        <div class="form-group">
          <label>API Key / Token (留空则保留当前已配置的 Key)</label>
          <input type="password" id="apiKey" placeholder="sk-...">
        </div>
      </div>
      <div class="btn-group" style="justify-content: flex-end;">
        <button class="secondary" onclick="testConnection()">🧪 测试模型联通性</button>
        <button onclick="saveAndApply()">💾 保存并应用到沙箱</button>
      </div>
    </div>

    <!-- 实时日志 -->
    <div class="card">
      <h2>Grok Bot 会话日志 <span style="font-size:12px; font-weight:normal; color:var(--text-muted);">(/tmp/opengrok-session.log)</span></h2>
      <div id="logBox" class="log-box">正在加载日志...</div>
    </div>
  </div>

  <script>
    const PRESETS = {
      'cpas': { url: 'https://api.deepseek.com', model: 'claude-sonnet-4-6' },
      'deepseek': { url: 'https://api.deepseek.com', model: 'deepseek-chat' },
      'deepseek-r1': { url: 'https://api.deepseek.com', model: 'deepseek-reasoner' },
      'glm': { url: 'https://open.bigmodel.cn/api/paas', model: 'glm-5.3-flash' },
      'openai': { url: 'https://api.openai.com', model: 'gpt-4o' },
      'claude': { url: 'https://openrouter.ai/api', model: 'anthropic/claude-3.7-sonnet' },
      'ollama': { url: 'http://127.0.0.1:11434', model: 'qwen2.5-coder' },
    };

    function setPreset(key) {
      const p = PRESETS[key];
      if (p) {
        document.getElementById('upstreamUrl').value = p.url;
        document.getElementById('modelSlug').value = p.model;
      }
    }

    function selectModel(name) {
      document.getElementById('modelSlug').value = name;
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
            badge.style.fontSize = '11px';
            badge.style.padding = '2px 8px';
            badge.textContent = m;
            badge.onclick = () => selectModel(m);
            badges.appendChild(badge);
          });
          showMsg(`✅ 成功获取 ${d.models.length} 个可用模型 (点击标签可快速选择)`, true);
        } else {
          showMsg('❌ 获取失败: ' + (d.error || '未返回可用模型'), false);
        }
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    function showMsg(text, isOk) {
      const box = document.getElementById('msgBox');
      box.textContent = text;
      box.className = isOk ? 'ok' : 'err';
      box.style.display = 'block';
      setTimeout(() => { box.style.display = 'none'; }, 6000);
    }

    async function refreshStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        document.getElementById('tsIp').textContent = data.tailscale_ip || '未知';
        
        const hostBadge = document.getElementById('hostBadge');
        const hostStatusText = document.getElementById('hostStatusText');
        if (data.sand_host && data.sand_host.running) {
          hostBadge.className = 'badge online';
          hostStatusText.textContent = 'Grok sand-host 运行中 (PID ' + data.sand_host.pids.join(', ') + ')';
          document.getElementById('sandHostPid').textContent = 'PID ' + data.sand_host.pids.join(', ');
        } else {
          hostBadge.className = 'badge offline';
          hostStatusText.textContent = 'Grok sand-host 未运行';
          document.getElementById('sandHostPid').textContent = '未运行';
        }

        const agent = (data.bindings && data.bindings.agents && (data.bindings.agents['*'] || Object.values(data.bindings.agents)[0])) || {};
        document.getElementById('curModel').textContent = agent.modelId || agent.name || '未配置';
        document.getElementById('curHop').textContent = agent.hopBaseUrl || '未配置';
        document.getElementById('curWrapStatus').textContent = data.wrapped ? '已注入 (createProtoSession)' : '未检测到注入';

        if (!document.getElementById('upstreamUrl').value && agent.upstream) {
          document.getElementById('upstreamUrl').value = agent.upstream;
        }
        if (!document.getElementById('modelSlug').value && agent.modelId) {
          document.getElementById('modelSlug').value = agent.modelId;
        }

        if (data.session_log) {
          const logBox = document.getElementById('logBox');
          logBox.textContent = data.session_log || '暂无最近请求日志。在 Grok Bot 中发送一条消息即可在此显示。';
          logBox.scrollTop = logBox.scrollHeight;
        }
      } catch (e) {
        console.error(e);
      }
    }

    async function testConnection() {
      const upstream = document.getElementById('upstreamUrl').value.trim();
      const model = document.getElementById('modelSlug').value.trim();
      const key = document.getElementById('apiKey').value.trim();
      if (!upstream || !model) {
        alert('请先填写上游 API 地址和模型名称');
        return;
      }
      showMsg('正在向上游发送探测请求...', true);
      try {
        const res = await fetch('/api/test', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ upstream, model, apiKey: key })
        });
        const d = await res.json();
        if (d.ok) {
          showMsg('✅ 测试通过: ' + (d.message || '模型响应正常'), true);
        } else {
          showMsg('❌ 测试失败: ' + (d.error || '无法连接上游'), false);
        }
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    async function saveAndApply() {
      const upstream = document.getElementById('upstreamUrl').value.trim();
      const model = document.getElementById('modelSlug').value.trim();
      const key = document.getElementById('apiKey').value.trim();
      if (!upstream || !model) {
        alert('请先填写上游 API 地址和模型名称');
        return;
      }
      showMsg('正在保存配置并应用...', true);
      try {
        const res = await fetch('/api/save', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ upstream, model, apiKey: key })
        });
        const d = await res.json();
        if (d.ok) {
          showMsg('✅ 配置已保存并热生效！sand-host 已重载。', true);
          refreshStatus();
        } else {
          showMsg('❌ 保存失败: ' + d.error, false);
        }
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    async function actionRestartHost() {
      showMsg('正在重启 sand-host 进程...', true);
      try {
        const res = await fetch('/api/restart-host', { method: 'POST' });
        const d = await res.json();
        showMsg(d.ok ? '✅ sand-host 重启成功' : '❌ 重启失败: ' + d.error, d.ok);
        setTimeout(refreshStatus, 1500);
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    async function actionReinstallWrap() {
      showMsg('正在执行原生沙箱注入与包装...', true);
      try {
        const res = await fetch('/api/install', { method: 'POST' });
        const d = await res.json();
        showMsg(d.ok ? '✅ Host 注入与包装已就绪' : '❌ 注入失败: ' + d.error, d.ok);
        refreshStatus();
      } catch (e) {
        showMsg('❌ 请求异常: ' + e.message, false);
      }
    }

    async function actionRunDoctor() {
      showMsg('正在运行 Doctor 诊断...', true);
      try {
        const res = await fetch('/api/doctor');
        const d = await res.json();
        alert(d.output || '诊断完成');
      } catch (e) {
        showMsg('❌ 诊断异常: ' + e.message, false);
      }
    }

    refreshStatus();
    setInterval(refreshStatus, 5000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "remote-dashboard/1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, html_str: str):
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._html(200, PAGE_HTML)
            return

        if self.path == "/api/status":
            ts_ip = get_tailscale_ip()
            sand_status = get_sand_host_status()
            bindings = get_bindings(DEFAULT_DATA)
            session_log = read_last_lines(SESSION_LOG, 40)
            wrapped = False
            if DEFAULT_HOST.is_file():
                try:
                    wrapped = "opengrok-runtime" in DEFAULT_HOST.read_text(encoding="utf-8")
                except Exception:
                    pass

            self._json(200, {
                "tailscale_ip": ts_ip,
                "sand_host": sand_status,
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

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            length = 0
        body = self.rfile.read(length) if length else b"{}"
        try:
            req_data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req_data = {}

        if self.path == "/api/models":
            upstream = req_data.get("upstream", "").rstrip("/")
            api_key = req_data.get("apiKey", "") or os.environ.get("API_SERVER_KEY", "")
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
                    resp_body = resp.read(1000).decode("utf-8", "replace")
                    self._json(200, {"ok": True, "status": resp.getcode(), "message": "200 OK - 模型响应正常"})
            except urllib.error.HTTPError as e:
                err_text = e.read(500).decode("utf-8", "replace")
                self._json(200, {"ok": False, "error": f"HTTP {e.code}: {err_text[:120]}"})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/save":
            upstream = req_data.get("upstream", "").rstrip("/")
            model = req_data.get("model", "")
            api_key = req_data.get("apiKey", "")
            if not upstream or not model:
                self._json(400, {"ok": False, "error": "upstream and model required"})
                return

            if api_key:
                os.environ["API_SERVER_KEY"] = api_key

            # Auto-wrap if host-main.cjs not yet wrapped
            try:
                if DEFAULT_HOST.is_file():
                    content = DEFAULT_HOST.read_text(encoding="utf-8", errors="replace")
                    if "createProtoSession" in content and "opengrok-runtime" not in content:
                        subprocess.run([sys.executable, str(HERE / "install-stock-box.py"), "--skip-hop"], capture_output=True)
            except Exception:
                pass

            # Write model-bindings.json
            data_dir = DEFAULT_DATA if DEFAULT_DATA.is_dir() else REPO_ROOT
            data_dir.mkdir(parents=True, exist_ok=True)
            bindings_path = data_dir / "model-bindings.json"
            hop_base = "http://127.0.0.1:18790/v1"

            bdoc = {
                "_comment": "configured via opengrok remote-dashboard",
                "agents": {
                    "*": {
                        "name": model,
                        "modelId": model,
                        "provider": "custom",
                        "hopBaseUrl": hop_base,
                        "upstream": upstream,
                    }
                }
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
    ap = argparse.ArgumentParser(description="Tailscale Web Control Dashboard for Grok Bot.")
    ap.add_argument("--port", type=int, default=8888, help="dashboard port (default: 8888)")
    ap.add_argument("--host", default="0.0.0.0", help="listen address (0.0.0.0 for Tailscale access)")
    args = ap.parse_args()

    ts_ip = get_tailscale_ip()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"""
╔═══════════════════════════════════════════════════════════════╗
║         Grok Bot / OpenGrok 远程控制面板已启动                 ║
╠═══════════════════════════════════════════════════════════════╣
║  • 本地访问:    http://127.0.0.1:{args.port:<5}                        ║
║  • Tailscale:   http://{ts_ip}:{args.port:<5}                  ║
╚═══════════════════════════════════════════════════════════════╝
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
