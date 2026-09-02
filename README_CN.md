# OpenGrok · Grok Bot 高级 AI 路由中继与多模型网关

<p align="center">
  <img src="assets/hero.png" alt="OpenGrok — Grok Bot 高级多模型分发中继网关" width="100%">
</p>

<p align="center">
  <b>🇨🇳 中文文档</b> | <a href="README.md"><b>English Documentation</b></a>
</p>

<p align="center">
  <a href="#-快速开始"><img alt="setup" src="https://img.shields.io/badge/安装-一键注入-7c6cff"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/协议-MIT-3fb950"></a>
  <a href="#-五层路由判定体系"><img alt="routing" src="https://img.shields.io/badge/路由-5层优先级体系-a78bfa"></a>
  <a href="#-零重启全链路热加载"><img alt="hot reload" src="https://img.shields.io/badge/热加载-零停机生效-f59e0b"></a>
  <img alt="deps" src="https://img.shields.io/badge/依赖-零外部依赖-2f81f7">
  <img alt="platform" src="https://img.shields.io/badge/平台-Linux%20%7C%20macOS%20%7C%20Windows-8b949e">
</p>

<p align="center">
  <b>自由将任意第三方 AI 模型（Claude 3.7、Gemini 3.7 Pro、DeepSeek-R1、GLM-5、GPT-4o、Ollama 等）无缝路由至 Grok Bot。</b><br>
  支持全链路零重启热加载、通配符拦截、多 Agent 独立分发、原生 Grok 免额度共存与自动容灾兜底。
</p>

---

## 📖 目录

- [项目背景与演化说明](#-项目背景与演化说明)
- [核心特性](#-核心特性)
- [五层路由判定体系](#-五层路由判定体系)
- [快速开始](#-快速开始)
- [远程控制台（Tailscale Web 控制中心）](#-远程控制台tailscale-web-控制中心)
- [模型映射与通配符拦截](#-模型映射与通配符拦截)
- [原生 Grok 并行与 Fail-Safe 容灾机制](#-原生-grok-并行与-fail-safe-容灾机制)
- [零重启全链路热加载](#-零重启全链路热加载)
- [自动化包装看门狗（Watchdog Daemon）](#-自动化包装看门狗watchdog-daemon)
- [测试与质量保障](#-测试与质量保障)
- [Fork 致谢与版权声明](#-fork-致谢与版权声明)
- [开源协议](#-开源协议)

---

## 🌟 项目背景与演化说明

**OpenGrok** 是专为 Grok Bot 云端沙箱环境设计的高性能、抗更新 AI 编排中间件。它透明地运行在 Grok Bot 宿主引擎（`host-main.cjs`）与各 AI 模型服务商之间，彻底摆脱 Cursor Cloud / 官方额度限制与模型锁死，将每一次对话回合自由分发到任意符合 OpenAI 兼容标准的 API 上游。

本项目最初基于开源项目 [OnlyTerp/opengrok](https://github.com/OnlyTerp/opengrok) 进行 Fork 与演化，并在其基础架构上进行了深度的企业级工程重构，构建了包括**零重启全热加载引擎、Tailscale 现代化控制中心、自动化抗篡改看门狗、多维度通配符路由引擎**等一系列核心演进特性。

---

## 🚀 核心特性

- ⚡ **全链路零重启热加载**：主力模型、Agent 绑定、模型映射与 API 密钥的任何修改均在下一个对话回合**即改即生效**，无需重启宿主进程，零停机且绝不掐断流式会话。
- 🧩 **五层超细粒度路由体系**：支持按 Agent UUID 专有绑定、精确模型 Slug 替换、通配符模式（`grok-4.5-*`、`sand-*`）、全局通配（`*`）与原生兜底自顶向下精准分发。
- 🛡️ **自动化看门狗守护（Watchdog Daemon）**：内置后台守护线程（默认 30 秒轮询，支持 `OPENGROK_WRAP_WATCHDOG_SEC` 自定义），秒级自动捕获沙箱进程重置行为并自动重注入 Hook。
- 🌀 **原生 Grok 并存与 Fail-Safe 容灾**：支持通过 `provider: "native"` 显式保留沙箱自带的 Grok-4.5/4.6 系列模型算力；当配置损坏或缺失时自动降级至原生 Grok，**永远不会断联报错**。
- 🌐 **现代化 Tailscale 远程控制台**：纯自研暗色 SaaS 风格 Web 控制中心，支持 Tailscale 内网状态探测、实名 Agent 解析下拉、实时会话日志流式高亮及移动端自适应。
- 🔒 **严格凭据隔离与 WAF 防护**：API Key 仅存于 mode-600 权限的沙箱本地文件，绝不随配置入库；自动伪装浏览器标准 User-Agent 绕过 Cloudflare WAF；采用 `os.replace` 原子写入杜绝 JSON 读写竞争。
- 🎯 **深度推理与动态思考控制**：完整适配 Claude 3.7 思考预算、DeepSeek-R1、Gemini 3.7 与 OpenAI o1/o3-mini 的 `reasoning_effort`；支持基于关键词上下文的动态深度调节（`effortWhen`）。

---

## 🏗️ 五层路由判定体系

```
                     Grok Bot 客户端发起的对话请求
                                  │
                                  ▼
           ┌──────────────────────────────────────────────┐
           │ 第 1 层: Agent UUID 专有绑定 (最高优先级)     │  例如: "taylor" 绑定到 GLM-5.3-Coding
           └──────────────────────┬───────────────────────┘
                                  │ (未命中)
           ┌──────────────────────▼───────────────────────┐
           │ 第 2 层: 模型 Slug 精确映射 (Exact Match)     │  例如: "gemini-2.5-flash" 替换为 Claude-3.7
           └──────────────────────┬───────────────────────┘
                                  │ (未命中)
           ┌──────────────────────▼───────────────────────┐
           │ 第 3 层: 模型 Slug 通配符匹配 (Wildcard Glob) │  例如: "grok-4.5-*" 拦截转发至 Gemini-3.7
           └──────────────────────┬───────────────────────┘
                                  │ (未命中)
           ┌──────────────────────▼───────────────────────┐
           │ 第 4 层: 全局通配默认规则 (*)                │  例如: "*" 默认走 DeepSeek-R1
           └──────────────────────┬───────────────────────┘
                                  │ (未配置 *)
           ┌──────────────────────▼───────────────────────┐
           │ 第 5 层: 沙箱原生 Grok 引擎 (安全兜底)        │  安全降级回退至沙箱内置原生 Grok 引擎
           └──────────────────────────────────────────────┘
```

---

## ⚡ 快速开始

### 1. 在 Grok Bot 沙箱主机上一键安装

直接在 Grok Bot 云端计算机终端中运行：

```bash
git clone https://github.com/xiongcp/opengrok.git
cd opengrok

# 设置您的上游 API Key（仅保存在沙箱 sand-data/.api_key，绝不入库）
export API_SERVER_KEY="your-api-key"

# 安装并注入 Hook 运行时
python3 tools/install-stock-box.py \
  --upstream https://api.your-provider.com/v1 \
  --model gemini-3.7-flash-high
```

在 Grok Bot 中发送任意消息。验证成功的标志为 `/tmp/opengrok-session.log` 中生成路由日志，且流量通过本地中继 `127.0.0.1:18790` 正确转发。

---

## 🌐 远程控制台（Tailscale Web 控制中心）

在沙箱主机上启动内置的 Web 控制面板：

```bash
python3 tools/remote-dashboard.py --port 8888 --host 0.0.0.0
```

在同一 Tailnet 内网中的手机或电脑浏览器访问 `http://<沙箱-Tailscale-IP>:8888`：

- **🌟 主力模型**：一键预设填充（Claude 3.7、DeepSeek-R1、Gemini 2.5/3.7、xAI Grok、OpenAI GPT-4o 等），支持实时连通性探测。
- **🤖 Agent 路由**：自动解析 `sand-data/agents/<uuid>/profile.json`，提供带实名标签（如 `taylor`、`New Bot`）的下拉配置。
- **🧩 模型映射**：直观配置原生 Slug 替换规则与 `*`、`?` 通配符模式。
- **⚡ 自适应深度（`effortWhen`）**：检测到 `refactor`、`audit` 等复杂任务关键词时自动拉升思考强度，检测到 `git diff`、`status` 等指令时自动调低。
- **🛠️ 系统运维**：直观展示 sand-host PID、hop 代理状态、包装状态与看门狗运行数据，并提供环境诊断。
- **📜 实时会话日志**：支持关键词过滤、日志级别筛选（路由/错误/工具调用/推理）与自动滚屏。

---

## 🧩 模型映射与通配符拦截

您可以精确指定当客户端请求某个特定模型时底层实际执行哪个模型：

编辑 `sand-data/model-bindings.json`（或在控制台【🧩 模型映射】页签中可视化配置）：

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

- 命中 `grok-4.5-*`（如 `grok-4.5-medium`、`grok-4.5-high`）的请求全部转发到 **GLM-5.3-Flash**。
- 请求 `grok-4.6` 显式保留为 **原生 Grok**，不消耗外部 API 额度。
- 沙箱内部的辅助标题生成会话（`sand-*`）静默升级至 **Gemini 3.7 Flash High**。

---

## 🌀 原生 Grok 并行与 Fail-Safe 容灾机制

为任意 Agent 或模型映射配置 `"provider": "native"` 即可启用原生通道：

1. **算力零浪费**：在接入外部顶级模型的同时，充分利用沙箱自带的 xAI Grok-4.5/4.6 算力。
2. **Fail-Safe 容灾兜底**：当绑定配置文件被误删、损坏或暂无规则时，OpenGrok 会自动降级为原生 Grok 处理，绝不向用户抛出崩溃异常。

---

## ⚡ 零重启全链路热加载

相较于早期版本，OpenGrok 实现了全流程的**热生效**机制：

- `opengrok-runtime.cjs` 在每个对话回合实时通过 `fs.readFileSync` 动态重读配置。
- `hop-server.py` 在收到每个 HTTP 请求时按需读取最新的上游 URL 与 API Key。
- 控制台点击保存或手动修改配置文件后，**无需执行任何进程重启**，下一个对话回合自动无缝应用最新配置。

---

## 🛡️ 自动化包装看门狗（Watchdog Daemon）

沙箱 Supervisor 在更新镜像或回收进程时，可能会用原始副本覆盖 `host-main.cjs` 导致 Hook 丢失。

OpenGrok 内置了自愈看门狗：

- 后台无感知运行（默认 30 秒轮询，可通过环境变量 `OPENGROK_WRAP_WATCHDOG_SEC` 调整）。
- 一旦检测到 Hook 丢失，自动执行非破坏性重新注入，绝不污染或覆盖用户现有的模型绑定。
- 自动向 Web 界面上报自愈次数与运行健康状态。

---

## 🧪 测试与质量保障

运行零回归全量测试套件：

```bash
# 全套 QA 自动化测试（包含接口契约、Hop 中继、运行时路由与安全泄漏扫描）
python3 tools/qa.py

# 单项测试套件
node tools/test-opengrok-runtime.cjs     # 五层路由优先级与原生兜底测试
python3 tools/test-hop-server.py          # /v1 URL 归一化与热读测试
node tools/test-provider-maps.cjs        # 契约 A 协议映射测试
node tools/test-provider-maps-hop.cjs    # 契约 B Hop 协议映射测试
python3 tools/test-wrap-proto-session.py # Prototype 包装 census 与注入测试
```

---

## 📜 Fork 致谢与版权声明

本项目系基于 [OnlyTerp/opengrok](https://github.com/OnlyTerp/opengrok)（基于 MIT License 开源，Copyright (c) 2026 OnlyTerp）进行的独立演进与增强开发。

我们衷心感谢原项目作者及社区贡献者所奠定的优秀基础：

- [OnlyTerp](https://github.com/OnlyTerp)：奠定了实证驱动的 Wire Map 协议映射与测试哲学。
- [simo255](https://github.com/simo255)：贡献了早期 Cloud Host 会话包装与流解析原型。

### 本分支（Fork）的核心演进成果

1. 构建了运行时与 Hop 中继的全链路**零重启热加载**架构。
2. 实现了支持通配符模式匹配与多维替换的 **五层路由优先级体系**。
3. 引入了 **原生 Grok (`provider: native`)** 并行与 Fail-Safe 容灾降级引擎。
4. 研发了自愈型 **自动化包装看门狗（Watchdog Daemon）**。
5. 设计并交付了全新的 **Tailscale 远程控制中心**，集成实名 Agent 探测、实时日志流与全响应式布局。

---

## 📄 开源协议

本项目遵循 [MIT License](LICENSE) 开源协议。
