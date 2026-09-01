# Remote Management & Tailscale Control Guide

This guide explains how to remotely install, configure, switch models, and operate Grok Bot using **Tailscale** and the built-in **Remote Web Dashboard** (`tools/remote-dashboard.py`).

---

## 1. Why Tailscale?

Grok Bot cloud computers (or remote VPS nodes) operate inside private environments without direct public IP access. Tailscale creates an encrypted peer-to-peer mesh network (Tailnet) between your laptop/phone and the Grok Bot host.

**Benefits:**

- **Zero Port Forwarding / Zero Public Exposure**: All traffic stays within your private 100.x.y.z Tailnet.
- **Remote Model Switching & Monitoring**: Update `model-bindings.json` and watch live Grok Bot conversation logs in real time from your browser.
- **One-Click Hot Reload**: Restart `sand-host` and re-inject host patches over the Web UI.

---

## 2. Launching the Remote Dashboard (On the Grok Bot Box)

In your Grok Bot terminal:

```bash
cd opengrok

# Start the dashboard on port 8888 (accessible over your Tailnet)
python3 tools/remote-dashboard.py --port 8888 --host 0.0.0.0
```

To run it continuously in the background:

```bash
nohup python3 tools/remote-dashboard.py --port 8888 --host 0.0.0.0 > /tmp/dashboard.log 2>&1 &
```

The console will output your Tailscale access URL:

```text
╔═══════════════════════════════════════════════════════════════╗
║         Grok Bot / OpenGrok 远程控制面板已启动                 ║
╠═══════════════════════════════════════════════════════════════╣
║  • 本地访问:    http://127.0.0.1:8888                         ║
║  • Tailscale:   http://<YOUR-TAILSCALE-IP>:8888               ║
╚═══════════════════════════════════════════════════════════════╝
```

---

## 3. Remote Operations via Web Browser

Open `http://<YOUR-TAILSCALE-IP>:8888` on your laptop or mobile browser connected to the same Tailscale account.

### Available Actions

1. **Preset & Model Selection**:
   - Quick presets for **DeepSeek (V3/R1)**, **智谱 GLM-5.3**, **OpenAI (GPT-4o)**, **Claude 3.7 (OpenRouter)**, and **Local Ollama**.
   - Input custom **Upstream Base URL** (e.g. `https://api.deepseek.com`, `https://api.openai.com`, `https://openrouter.ai/api`).
   - Enter your `API_SERVER_KEY` (injected into hop memory, never leaked to disk/git).
2. **🧪 Test Model Connectivity**:
   - Sends a live request directly from the box to verify 200 OK before committing changes.
3. **💾 Save & Apply**:
   - Writes configuration, refreshes `hop-server.py`, and automatically bounces `sand-host`.
4. **⚡ Host Re-wrap (After Grok Bot Updates)**:
   - If a Grok Bot update overwrites `host-main.cjs`, click **"重新注入 Host 包装"** to re-apply the `createProtoSession` wrapper instantly.
5. **📜 Live Session Logs**:
   - Real-time stream of `/tmp/opengrok-session.log` showing conversation turns hitting your custom model.

---

## 4. CLI Alternative (Zero Web UI)

If you prefer CLI over SSH via Tailscale:

```bash
# 1. Export API key
export API_SERVER_KEY="your-api-key-here"

# 2. Run stock host installer
python3 tools/install-stock-box.py \
  --upstream https://api.deepseek.com \
  --model deepseek-chat

# 3. Bounce host
pkill -f "host-main.cjs"

# 4. Tail session log
tail -f /tmp/opengrok-session.log
```
