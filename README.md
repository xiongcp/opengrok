<p align="center">
  <img src="assets/hero.png" alt="OpenGrok — Advanced AI Middleware & Multi-Model Gateway for Grok Bot" width="100%">
</p>

<p align="center">
  <a href="README_CN.md"><b>🇨🇳 中文文档</b></a> | <b>English Documentation</b>
</p>

<p align="center">
  <a href="#-quick-start"><img alt="setup" src="https://img.shields.io/badge/setup-one%20command-7c6cff"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-3fb950"></a>
  <a href="#-architecture--the-5-layer-routing-hierarchy"><img alt="routing" src="https://img.shields.io/badge/routing-5--layer%20hierarchy-a78bfa"></a>
  <a href="#-zero-downtime-hot-reloading"><img alt="hot reload" src="https://img.shields.io/badge/hot--reload-zero%20downtime-f59e0b"></a>
  <img alt="deps" src="https://img.shields.io/badge/dependencies-zero-2f81f7">
  <img alt="platform" src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-8b949e">
</p>

<p align="center">
  <b>Route any AI model to Grok Bot with zero restarts, wildcard slug matching, multi-agent dispatch, and native fallback.</b><br>
  Keys stay in private secure storage, never hardcoded in configurations or repositories.
</p>

---

## 📖 Table of Contents

- [Overview & Evolution](#-overview--evolution)
- [Key Features](#-key-features)
- [Architecture & The 5-Layer Routing Hierarchy](#-architecture--the-5-layer-routing-hierarchy)
- [Quick Start](#-quick-start)
- [Remote Management (Tailscale Web Console)](#-remote-management-tailscale-web-console)
- [Model Mapping & Wildcard Interception](#-model-mapping--wildcard-interception)
- [Native Grok Bypass & Fail-Safe Mode](#-native-grok-bypass--fail-safe-mode)
- [Zero-Downtime Hot Reloading](#-zero-downtime-hot-reloading)
- [Automated Watchdog Daemon](#-automated-watchdog-daemon)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Fork Attribution & Copyright Notice](#-fork-attribution--copyright-notice)
- [License](#-license)

---

## 🌟 Overview & Evolution

**OpenGrok** is a high-performance, update-resilient AI orchestration middleware designed for Grok Bot cloud environments. It sits transparently between the Grok Bot host engine (`host-main.cjs`) and upstream AI providers, allowing you to bypass vendor lock-in, eliminate quota constraints, and route requests to any custom OpenAI-compatible API (including Claude 3.7, Gemini 3.7 Pro, DeepSeek-R1, GLM-5, GPT-4o, and local Ollama/vLLM endpoints).

This project originated as an enhanced fork of [OnlyTerp/opengrok](https://github.com/OnlyTerp/opengrok) and has evolved into an autonomous, enterprise-grade AI middleware system featuring full zero-restart hot-reloading, a modern Tailscale Web Console, automated anti-tamper host injection watchdogs, and multi-dimensional wildcard routing.

---

## 🚀 Key Features

- ⚡ **Zero-Downtime Hot Reloading**: Update models, API endpoints, tokens, and routing rules on-the-fly. Next-turn dynamic evaluation means zero downtime and no dropped streams.
- 🧩 **5-Layer Multi-Dimensional Routing Hierarchy**: Dispatches requests by Agent UUID, exact model slug, glob wildcards (`grok-4.5-*`, `sand-*`), global fallback (`*`), or native bypass.
- 🛡️ **Autonomous Watchdog Daemon**: Background thread automatically detects when the sandbox supervisor resets `host-main.cjs` to a stock image and re-injects OpenGrok hooks.
- 🌀 **Native Grok Coexistence & Fail-Safe**: Keep native xAI Grok-4.5/4.6 models active alongside external third-party models using `provider: "native"`. Corrupted or missing configs gracefully degrade to native Grok instead of crashing.
- 🌐 **Tailscale Web Control Center**: Modern SaaS-style dark UI with real-time process monitoring, live session log inspection, preset model selectors, and named Agent pickers.
- 🔒 **Enterprise-Grade Security & WAF Bypass**: Strict credential isolation with mode-600 files, browser-standard User-Agent rotation to bypass Cloudflare WAF, and atomic file transactions (`os.replace`) to prevent corrupted reads.
- 🎯 **Deep Reasoning & Thinking Control**: Seamlessly translate thinking parameters, extended thinking budgets, and dynamic keyword-triggered thinking (`effortWhen`) across all upstream dialects.

---

## 🏗️ Architecture & The 5-Layer Routing Hierarchy

```
                    Incoming Grok Bot Client Request
                                  │
                                  ▼
           ┌──────────────────────────────────────────────┐
           │ Layer 1: Agent UUID Specific Binding         │  (e.g., "taylor" -> GLM-5.3-Coding)
           └──────────────────────┬───────────────────────┘
                                  │ (No match)
           ┌──────────────────────▼───────────────────────┐
           │ Layer 2: Exact Model Slug Mapping            │  (e.g., "gemini-2.5-flash" -> Claude-3.7)
           └──────────────────────┬───────────────────────┘
                                  │ (No match)
           ┌──────────────────────▼───────────────────────┐
           │ Layer 3: Wildcard Glob Pattern Matching      │  (e.g., "grok-4.5-*" -> Gemini-3.7-Pro)
           └──────────────────────┬───────────────────────┘
                                  │ (No match)
           ┌──────────────────────▼───────────────────────┐
           │ Layer 4: Global Wildcard Default (*)         │  (e.g., "*" -> DeepSeek-R1)
           └──────────────────────┬───────────────────────┘
                                  │ (Unset)
           ┌──────────────────────▼───────────────────────┐
           │ Layer 5: Native Stock Passthrough            │  (Degrades safely to sandbox stock Grok)
           └──────────────────────────────────────────────┘
```

---

## ⚡ Quick Start

### 1. Installation on Grok Bot Sandbox (Recommended)

Run directly on the Grok Bot cloud computer:

```bash
git clone https://github.com/xiongcp/opengrok.git
cd opengrok

# Set your upstream provider key (stored in sand-data/.api_key, never in configs)
export API_SERVER_KEY="your-api-key"

# Install and inject the runtime wrapper
python3 tools/install-stock-box.py \
  --upstream https://api.your-provider.com/v1 \
  --model gemini-3.7-flash-high
```

Send a message in Grok Bot. Verification proof is visible in `/tmp/opengrok-session.log` and traffic routed via `127.0.0.1:18790`.

---

## 🌐 Remote Management (Tailscale Web Console)

Launch the built-in Web Control Panel on the sandbox host:

```bash
python3 tools/remote-dashboard.py --port 8888 --host 0.0.0.0
```

Open `http://<tailscale-ip>:8888` on your phone or laptop connected to the same Tailnet:

- **🌟 Primary Model**: One-click configuration with presets for Claude 3.7, DeepSeek-R1, Gemini 2.5/3.7, xAI Grok, and OpenAI GPT-4o.
- **🤖 Agent Routing**: View named agents extracted automatically from `sand-data/agents/<uuid>/profile.json`.
- **🧩 Model Mapping**: Configure model-level substitution and wildcard matching rules.
- **⚡ Adaptive Depth (`effortWhen`)**: Automatically scale reasoning effort up on complex keywords (e.g. `refactor`, `audit`, `security`) and down on status commands (`git diff`, `test`).
- **🛠️ System Diagnostics**: Real-time status badges for sand-host, hop proxy, wrap status, and the watchdog daemon.
- **📜 Live Session Logs**: Real-time log streamer with filter chips (Route, Error, Tool, Effort).

---

## 🧩 Model Mapping & Wildcard Interception

Take control over what happens when the client asks for specific model slugs:

Edit `sand-data/model-bindings.json` (or use the **🧩 Model Mapping** tab in the Web UI):

```json
{
  "agents": {
    "*": {
      "modelId": "gemini-3.7-flash-high",
      "provider": "custom",
      "hopBaseUrl": "http://127.0.0.1:18790/v1",
      "parameters": [
        { "id": "effort", "value": "high" },
        { "id": "thinking", "value": "true" }
      ]
    }
  },
  "models": {
    "grok-4.5-*": {
      "modelId": "glm-5.3-flash",
      "provider": "custom",
      "hopBaseUrl": "http://127.0.0.1:18790/v1"
    },
    "grok-4.6": {
      "provider": "native"
    },
    "sand-*": {
      "modelId": "gemini-3.7-flash-high",
      "provider": "custom",
      "hopBaseUrl": "http://127.0.0.1:18790/v1"
    }
  }
}
```

- Any request matching `grok-4.5-*` (e.g., `grok-4.5-medium`, `grok-4.5-high`) routes to **GLM-5.3-Flash**.
- Requests for `grok-4.6` stay **Native** without consuming third-party API quotas.
- Background auxiliary tasks (`sand-*`) route to **Gemini 3.7 Flash High**.

---

## 🌀 Native Grok Bypass & Fail-Safe Mode

Setting `"provider": "native"` on any agent or model mapping explicitly routes calls to the stock sandbox engine:

1. **Zero Resource Waste**: Leverage sandbox-bundled xAI Grok-4.5/4.6 compute alongside external models.
2. **Fail-Safe Passthrough**: If bindings are missing, unparseable, or invalid, OpenGrok automatically degrades to the native engine rather than crashing in-flight user sessions.

---

## ⚡ Zero-Downtime Hot Reloading

Unlike legacy setups, OpenGrok **never requires process restarts** for everyday configuration changes:

- `opengrok-runtime.cjs` performs fresh dynamic file reads on each conversation turn.
- `hop-server.py` hot-reads upstream endpoints and authorization keys per HTTP request.
- Changes applied via the Web Dashboard or file edits take effect immediately on the next message turn.

---

## 🛡️ Automated Watchdog Daemon

Cloud sandbox supervisors frequently restore stock copies of `host-main.cjs` during updates or process recycling, silently wiping user patches.

OpenGrok includes an embedded Watchdog Daemon:
- Runs continuously in the background (default 30-second interval, configurable via `OPENGROK_WRAP_WATCHDOG_SEC`).
- Detects unwrapped host files and automatically executes an atomic re-wrap without touching user configurations.
- Reports health, last check timestamp, and total re-wrap events directly to the Web UI.

---

## 🧪 Testing & Quality Assurance

Run the comprehensive zero-regression test suite locally or on the box:

```bash
# Full test suite (contract tests, hop session, runtime routing, and leak scan)
python3 tools/qa.py

# Specialized test runners
node tools/test-opengrok-runtime.cjs     # 5-layer precedence matrix & native routing
python3 tools/test-hop-server.py          # /v1 URL joining & per-request hot-read
node tools/test-provider-maps.cjs        # Contract A wire maps
node tools/test-provider-maps-hop.cjs    # Contract B hop wire maps
python3 tools/test-wrap-proto-session.py # Prototype wrapper census & AST injector
```

---

## 📜 Fork Attribution & Copyright Notice

This project is an advanced, independent evolution developed from [OnlyTerp/opengrok](https://github.com/OnlyTerp/opengrok) (MIT License, Copyright (c) 2026 OnlyTerp).

We gratefully acknowledge the original architecture and community contributions, including:

- Foundational wire mapping concepts and evidence-based testing philosophy by [OnlyTerp](https://github.com/OnlyTerp).
- Cloud host session wrapping and stream parsing prototypes by [simo255](https://github.com/simo255).

### Key Architectural Advancements in this Fork

1. Full zero-downtime hot-reloading architecture for both runtime hooks and proxy relays.
2. 5-Layer routing hierarchy supporting wildcard glob slug matching and per-model substitutions.
3. Native Grok coexistence (`provider: "native"`) and fail-safe fallback engine.
4. Autonomous anti-tamper Watchdog Daemon.
5. Modern Tailscale Web Control Center with real-time log streaming, named agent extraction, and mobile responsiveness.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
