"use strict";
/*
 * OpenAI hop session. This file was documented as shipping on the box and
 * never existed in the public repo (issues #1, #3, #5).
 *
 * Hosts that already had a private copy can keep using apply-box-patch.py.
 * Stock hosts load this via opengrok-runtime.cjs after wrap_proto_session.py.
 */
var http = require("http");
var https = require("https");
var path = require("path");
var { URL } = require("url");

function completionsUrl(baseUrl) {
  var b = String(baseUrl || "").replace(/\/+$/, "");
  if (!b) throw new Error("openai-hop-session: missing baseUrl");
  if (/\/chat\/completions$/i.test(b)) return b;
  if (/\/v1$/i.test(b)) return b + "/chat/completions";
  return b + "/v1/chat/completions";
}

function loadMaps() {
  var candidates = [
    "/home/box/sand-data/provider-maps-hop.cjs",
    path.join(__dirname, "provider-maps-hop.cjs"),
    "/home/box/sand-data/provider-maps.cjs",
    path.join(__dirname, "provider-maps.cjs"),
  ];
  for (var i = 0; i < candidates.length; i++) {
    try {
      return require(candidates[i]);
    } catch (_err) {
      void _err;
    }
  }
  return null;
}

function applyMaps(body, ctx) {
  ctx = ctx || {};
  var maps = loadMaps();
  if (maps) {
    if (typeof maps.applyHarnessControls === "function") {
      var res = maps.applyHarnessControls({
        modelId: ctx.modelId,
        baseUrl: ctx.baseUrl,
        maxMode: ctx.maxMode,
        parameters: ctx.parameters,
        body: body,
      });
      if (res && res.body) {
        Object.assign(body, res.body);
      }
    } else if (typeof maps.applyProviderReasoningControls === "function") {
      var localQwen = false;
      if (!localQwen) {
        maps.applyProviderReasoningControls(body, ctx);
      }
    }
  }

  // Guaranteed fallback: ensure reasoning_effort is set if parameters define it
  var params = Array.isArray(ctx.parameters) ? ctx.parameters : [];
  var effort = null;
  var fast = false;
  var thinking = null;
  for (var i = 0; i < params.length; i++) {
    var p = params[i];
    if (!p) continue;
    if (p.id === "effort" && p.value != null) effort = String(p.value);
    if (p.id === "fast" && (p.value === true || String(p.value) === "true"))
      fast = true;
    if (p.id === "thinking" && p.value != null) thinking = String(p.value);
  }

  if (body.reasoning_effort == null) {
    if (fast || thinking === "false") {
      body.reasoning_effort = "low";
    } else if (effort) {
      body.reasoning_effort = effort;
    } else if (thinking === "true" || ctx.maxMode === true) {
      body.reasoning_effort = "high";
    }
  }
}

function mergeToolCallDelta(acc, tcArray) {
  if (!Array.isArray(tcArray)) return;
  for (var i = 0; i < tcArray.length; i++) {
    var tc = tcArray[i];
    var idx = tc.index == null ? i : tc.index;
    if (!acc.tool_calls[idx]) {
      acc.tool_calls[idx] = {
        id: tc.id || "",
        type: tc.type || "function",
        function: { name: "", arguments: "" },
      };
    }
    var slot = acc.tool_calls[idx];
    if (tc.id) slot.id = tc.id;
    if (tc.function) {
      if (tc.function.name) slot.function.name = tc.function.name;
      if (tc.function.arguments)
        slot.function.arguments += tc.function.arguments;
    }
  }
}

function buildTurnOut(acc) {
  var toolCalls = [];
  for (var i = 0; i < acc.tool_calls.length; i++) {
    if (acc.tool_calls[i]) toolCalls.push(acc.tool_calls[i]);
  }
  return {
    content: acc.content,
    reasoning_content: acc.reasoning_content,
    tool_calls: toolCalls,
    finish_reason: acc.finish_reason,
    raw: acc.raw,
  };
}

function postJson(urlStr, body, headers, timeoutMs, session) {
  return new Promise((resolve, reject) => {
    var u;
    try {
      u = new URL(urlStr);
    } catch (e) {
      return reject(new Error("openai-hop-session: invalid url: " + e.message));
    }
    var lib = u.protocol === "https:" ? https : http;
    var payload = Buffer.from(JSON.stringify(body), "utf8");
    var hdrs = Object.assign(
      {
        "Content-Type": "application/json",
        "Content-Length": String(payload.length),
        Accept: "application/json",
        "User-Agent": "OpenGrok/1.0 (Mozilla/5.0)",
      },
      headers || {},
    );
    var req = lib.request(
      {
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port || (u.protocol === "https:" ? 443 : 80),
        path: u.pathname + u.search,
        method: "POST",
        headers: hdrs,
      },
      (res) => {
        var chunks = [];
        res.on("data", (c) => {
          chunks.push(c);
        });
        res.on("end", () => {
          var raw = Buffer.concat(chunks).toString("utf8");
          var json = null;
          try {
            json = JSON.parse(raw);
          } catch (_errJson) {
            void _errJson;
            json = null;
          }
          resolve({ status: res.statusCode, raw: raw, json: json });
        });
      },
    );
    if (session) session._activeReq = req;
    req.setTimeout(timeoutMs || 180000, () => {
      req.destroy();
      reject(new Error("openai-hop-session: upstream timeout"));
    });
    req.on("error", (err) => {
      if (session && session._activeReq === req) session._activeReq = null;
      if (err && (err.code === "ECONNRESET" || err.message === "aborted")) {
        reject(new Error("openai-hop-session: aborted"));
        return;
      }
      reject(err);
    });
    req.on("close", () => {
      if (session && session._activeReq === req) session._activeReq = null;
    });
    req.write(payload);
    req.end();
  });
}

function postStream(urlStr, body, headers, timeoutMs, session, gen, onDelta) {
  return new Promise((resolve, reject) => {
    var u;
    try {
      u = new URL(urlStr);
    } catch (e) {
      return reject(new Error("openai-hop-session: invalid url: " + e.message));
    }
    var lib = u.protocol === "https:" ? https : http;
    var payload = Buffer.from(JSON.stringify(body), "utf8");
    var hdrs = Object.assign(
      {
        "Content-Type": "application/json",
        "Content-Length": String(payload.length),
        Accept: "text/event-stream",
        "User-Agent": "OpenGrok/1.0 (Mozilla/5.0)",
      },
      headers || {},
    );
    var acc = {
      content: "",
      reasoning_content: "",
      tool_calls: [],
      finish_reason: null,
      raw: { id: "", usage: null },
    };
    var buf = "";
    var req = lib.request(
      {
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port || (u.protocol === "https:" ? 443 : 80),
        path: u.pathname + u.search,
        method: "POST",
        headers: hdrs,
      },
      (res) => {
        if (res.statusCode < 200 || res.statusCode >= 300) {
          var errChunks = [];
          res.on("data", (c) => {
            errChunks.push(c);
          });
          res.on("end", () => {
            var raw = Buffer.concat(errChunks).toString("utf8");
            reject(
              new Error(
                "openai-hop-session: HTTP " +
                  res.statusCode +
                  " " +
                  raw.slice(0, 300),
              ),
            );
          });
          return;
        }
        res.on("data", (chunk) => {
          buf += chunk.toString("utf8");
          var parts = buf.split("\n");
          buf = parts.pop() || "";
          for (var i = 0; i < parts.length; i++) {
            var line = parts[i].trim();
            if (!line || line.indexOf("data:") !== 0) continue;
            var data = line.slice(5).trim();
            if (!data || data === "[DONE]") continue;
            try {
              var json = JSON.parse(data);
              if (json.id) acc.raw.id = json.id;
              if (json.usage) acc.raw.usage = json.usage;
              var choice = json.choices && json.choices[0];
              if (!choice) continue;
              if (choice.finish_reason)
                acc.finish_reason = choice.finish_reason;
              var delta = choice.delta || {};
              if (delta.reasoning_content) {
                acc.reasoning_content += delta.reasoning_content;
                if (onDelta)
                  onDelta({
                    type: "reasoning",
                    textDelta: delta.reasoning_content,
                  });
              }
              if (delta.content) {
                acc.content += delta.content;
                if (onDelta)
                  onDelta({ type: "text", textDelta: delta.content });
              }
              if (delta.tool_calls) mergeToolCallDelta(acc, delta.tool_calls);
            } catch (_errSse) {
              void _errSse;
              /* skip malformed SSE line */
            }
          }
        });
        res.on("end", () => {
          resolve(buildTurnOut(acc));
        });
      },
    );
    if (session) session._activeReq = req;
    req.setTimeout(timeoutMs || 180000, () => {
      req.destroy();
      reject(new Error("openai-hop-session: upstream timeout"));
    });
    req.on("error", (err) => {
      if (session && session._activeReq === req) session._activeReq = null;
      if (gen !== session._turnGen) {
        reject(new Error("openai-hop-session: aborted"));
        return;
      }
      if (err && (err.code === "ECONNRESET" || err.message === "aborted")) {
        reject(new Error("openai-hop-session: aborted"));
        return;
      }
      reject(err);
    });
    req.on("close", () => {
      if (session && session._activeReq === req) session._activeReq = null;
    });
    req.write(payload);
    req.end();
  });
}

function coerceContent(content) {
  if (content == null) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((p) => {
        if (typeof p === "string") return p;
        if (p && p.type === "text") return p.text || "";
        return "";
      })
      .join("");
  }
  return String(content);
}

function OpenAiHopSession(opts) {
  opts = opts || {};
  this.requestKind = opts.requestKind;
  this.maxMode = opts.maxMode === true;
  this.parameters = Array.isArray(opts.parameters) ? opts.parameters : [];
  this.modelId = opts.modelId || opts.model;
  this.baseUrl = opts.baseUrl || opts.openaiBaseUrl || opts.hopBaseUrl;
  this.apiKey = opts.apiKey || process.env.API_SERVER_KEY || "";
  this.allowTestVisibleRecovery = opts.allowTestVisibleRecovery === true;
  this.streamHop = opts.streamHop !== false;
  this._turnGen = 0;
  this._cancelNext = false;
  this._activeReq = null;
  this.opengrok = true;
}

OpenAiHopSession.prototype.abort = function abort() {
  this._turnGen++;
  if (this._activeReq) {
    this._activeReq.destroy();
    this._activeReq = null;
  } else {
    this._cancelNext = true;
  }
};

OpenAiHopSession.prototype.getThinkingDetails = function getThinkingDetails() {
  return undefined;
};

OpenAiHopSession.prototype._headers = function _headers() {
  var h = {};
  if (this.apiKey) h.Authorization = "Bearer " + this.apiKey;
  return h;
};

OpenAiHopSession.prototype._body = function _body(turn, stream) {
  turn = turn || {};
  var messages = turn.messages;
  if (!Array.isArray(messages))
    messages = [
      { role: "user", content: String(turn.content || turn.prompt || "") },
    ];
  var body = {
    model: this.modelId,
    messages: messages,
    stream: stream === true,
  };
  if (Array.isArray(turn.tools) && turn.tools.length) body.tools = turn.tools;
  body.max_tokens = turn.max_tokens == null ? 8192 : turn.max_tokens;
  applyMaps(body, {
    modelId: this.modelId,
    baseUrl: this.baseUrl,
    maxMode: this.maxMode,
    parameters: this.parameters,
  });
  return body;
};

OpenAiHopSession.prototype.runTurn = function runTurn(turn) {
  if (this._cancelNext) {
    this._cancelNext = false;
    return Promise.reject(new Error("openai-hop-session: aborted"));
  }
  var gen = this._turnGen;
  var body = this._body(turn, false);
  var url = completionsUrl(this.baseUrl);
  return postJson(url, body, this._headers(), 180000, this).then((res) => {
    if (this._turnGen !== gen) throw new Error("openai-hop-session: aborted");
    if (res.status < 200 || res.status >= 300) {
      var msg = "openai-hop-session: HTTP " + res.status;
      if (res.raw) msg += " " + res.raw.slice(0, 300);
      throw new Error(msg);
    }
    var choice = res.json && res.json.choices && res.json.choices[0];
    var message = (choice && choice.message) || {};
    return {
      content: coerceContent(message.content),
      reasoning_content: message.reasoning_content || "",
      tool_calls: Array.isArray(message.tool_calls) ? message.tool_calls : [],
      finish_reason: choice && choice.finish_reason,
      raw: res.json,
    };
  });
};

OpenAiHopSession.prototype.runTurnStream = function runTurnStream(
  turn,
  onDelta,
) {
  if (this._cancelNext) {
    this._cancelNext = false;
    return Promise.reject(new Error("openai-hop-session: aborted"));
  }
  var gen = this._turnGen;
  var body = this._body(turn, true);
  var url = completionsUrl(this.baseUrl);
  return postStream(
    url,
    body,
    this._headers(),
    180000,
    this,
    gen,
    onDelta,
  ).then((out) => {
    if (this._turnGen !== gen) throw new Error("openai-hop-session: aborted");
    return out;
  });
};

OpenAiHopSession.prototype.stream = function stream(turn, handlers) {
  handlers = handlers || {};
  return this.runTurnStream(turn, (ev) => {
    if (ev.type === "reasoning" && typeof handlers.onReasoning === "function")
      handlers.onReasoning(ev.textDelta);
    if (ev.type === "text" && typeof handlers.onText === "function")
      handlers.onText(ev.textDelta);
  }).then(
    (out) => {
      if (typeof handlers.onDone === "function") handlers.onDone(out);
      return out;
    },
    (err) => {
      if (typeof handlers.onError === "function") handlers.onError(err);
      throw err;
    },
  );
};

function createOpenAiHopSession(opts) {
  var requestKind = opts && opts.requestKind;
  var maxMode = (opts && opts.maxMode) === true;
  var parameters = Array.isArray(opts && opts.parameters)
    ? opts.parameters
    : [];
  return new OpenAiHopSession({
    requestKind: requestKind,
    maxMode: maxMode,
    parameters: parameters,
    modelId: opts && (opts.modelId || opts.model),
    baseUrl: opts && (opts.baseUrl || opts.openaiBaseUrl || opts.hopBaseUrl),
    apiKey: opts && opts.apiKey,
    allowTestVisibleRecovery: opts && opts.allowTestVisibleRecovery === true,
    streamHop: opts && opts.streamHop !== false,
  });
}

module.exports = {
  createOpenAiHopSession: createOpenAiHopSession,
  completionsUrl: completionsUrl,
  OpenAiHopSession: OpenAiHopSession,
};
