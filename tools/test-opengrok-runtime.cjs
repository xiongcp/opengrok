"use strict";
var assert = require("assert");
var rt = require("./opengrok-runtime.cjs");
var h = rt._testHooks;

function hasMeaningfulContentPart(part) {
  if (part.type === "text") return part.text.trim().length > 0;
  if (part.type === "reasoning") return part.text.trim().length > 0;
  return part.type === "tool-call" || part.type === "file";
}

// GLM sometimes emits tool calls as embedded JSON objects in the text stream.
var embedded = h.extractEmbeddedStreamJson(
  '{"type":"tool-call","toolName":"SendToUser","args":{"type":"text","content":"hi"}}',
);
assert.equal(embedded.calls.length, 1);
assert.equal(embedded.calls[0].function.name, "SendToUser");
assert.equal(embedded.text, "");

var mixed = h.extractEmbeddedStreamJson(
  'hello {"type":"reasoning","text":"think"} {"type":"tool-call","toolName":"send_message","args":{"x":1}}',
);
assert.equal(mixed.text, "hello");
assert.equal(mixed.reasoning, "think");
assert.equal(mixed.calls.length, 1);
assert.equal(mixed.calls[0].function.name, "SendToUser");

// Host treats text-only assistant responses as empty when tools were expected.
var content = h.buildAssistantResponseContent(
  { reasoning_content: "chain" },
  "visible",
  [
    {
      id: "c1",
      function: {
        name: "SendToUser",
        arguments: '{"type":"text","content":"ok"}',
      },
    },
  ],
);
assert.ok(Array.isArray(content));
assert.equal(
  content.filter(function (p) {
    return p.type === "reasoning";
  }).length,
  1,
);
assert.equal(
  content.filter(function (p) {
    return p.type === "text";
  }).length,
  1,
);
assert.equal(
  content.filter(function (p) {
    return p.type === "tool-call";
  }).length,
  1,
);
assert.ok(content.every(hasMeaningfulContentPart));

// Round-trip assistant tool history into OpenAI wire shape.
var msgs = h.toOpenAIMessages([
  {
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "tc1",
        toolName: "SendToUser",
        args: { type: "text", content: "x" },
      },
    ],
  },
  {
    role: "tool",
    toolCallId: "tc1",
    content: [{ type: "tool-result", result: "done" }],
  },
]);
assert.equal(msgs.length, 2);
assert.equal(msgs[0].role, "assistant");
assert.ok(Array.isArray(msgs[0].tool_calls));
assert.equal(msgs[0].tool_calls[0].function.name, "SendToUser");
assert.equal(msgs[1].role, "tool");
assert.equal(msgs[1].tool_call_id, "tc1");
assert.equal(msgs[1].content, "done");

var effortBinding = {
  parameters: [{ id: "effort", value: "high" }],
  effortWhen: {
    medium: ["run_native_sot", "post_screen.py", "backtest_results"],
  },
};
var researchParams = h.resolveParametersForTurn(effortBinding, [
  { role: "user", content: "read LEDGER.tsv and mills.yaml" },
]);
var analyzeParams = h.resolveParametersForTurn(effortBinding, [
  { role: "user", content: "now run_native_sot.py with the session config" },
]);
assert.equal(
  researchParams.filter(function (p) {
    return p.id === "effort";
  })[0].value,
  "high",
);
assert.equal(
  analyzeParams.filter(function (p) {
    return p.id === "effort";
  })[0].value,
  "medium",
);

// --- wrapSession routing: native lane + fail-safe passthrough ---
// (bindings path is captured at require time, so re-require with env set)
var path = require("path");
var os = require("os");
var fs2 = require("fs");

function rerequire(bindingsPath) {
  process.env.OPENGROK_BINDINGS = bindingsPath;
  process.env.OPENGROK_LOG = path.join(
    os.tmpdir(),
    "opengrok-test-session.log",
  );
  delete require.cache[require.resolve("./opengrok-runtime.cjs")];
  return require("./opengrok-runtime.cjs");
}
var stockSentinel = {
  kind: "stock",
  getSession: function () {
    return {};
  },
};
var stockFn = function () {
  return stockSentinel;
};
var turnArgs = [{}, { modelId: "grok-4.6" }];

// 1. No bindings file at all -> native passthrough (previously threw).
var rtNone = rerequire(path.join(os.tmpdir(), "opengrok-test-missing.json"));
assert.equal(
  rtNone.wrapSession(stockFn, turnArgs),
  stockSentinel,
  "no bindings file must degrade to native, never kill chat",
);

// 2. provider:"native" binding -> passthrough even though it has no hop fields.
var tmpB = path.join(os.tmpdir(), "opengrok-test-bindings.json");
fs2.writeFileSync(
  tmpB,
  JSON.stringify({ agents: { "*": { name: "native", provider: "native" } } }),
);
var rtNative = rerequire(tmpB);
assert.equal(
  rtNative.wrapSession(stockFn, turnArgs),
  stockSentinel,
  "provider=native must return the stock provider untouched",
);

// 3. A hop binding still routes through the hop wrapper.
fs2.writeFileSync(
  tmpB,
  JSON.stringify({
    agents: {
      "*": {
        modelId: "glm-5.3-flash",
        provider: "custom",
        hopBaseUrl: "http://127.0.0.1:9/v1",
      },
    },
  }),
);
var rtHop = rerequire(tmpB);
var wrapped = rtHop.wrapSession(stockFn, turnArgs);
assert.notEqual(wrapped, stockSentinel);
assert.equal(wrapped.opengrok, true);
assert.equal(wrapped.getModelId(), "glm-5.3-flash");

// 4. models map: models[slug] beats agents["*"]; native pin; unmapped -> "*".
fs2.writeFileSync(
  tmpB,
  JSON.stringify({
    agents: {
      "*": {
        modelId: "gemini-3.7-flash-high",
        provider: "custom",
        hopBaseUrl: "http://127.0.0.1:9/v1",
      },
    },
    models: {
      "grok-4.5-high": {
        modelId: "glm-5.3-flash",
        provider: "custom",
        hopBaseUrl: "http://127.0.0.1:9/v1",
      },
      "grok-4.6": { provider: "native" },
    },
  }),
);
var rtModels = rerequire(tmpB);
var m1 = rtModels.wrapSession(stockFn, [{}, { modelId: "grok-4.5-high" }]);
assert.equal(
  m1.getModelId(),
  "glm-5.3-flash",
  "models[slug] must beat agents['*']",
);
assert.equal(
  rtModels.wrapSession(stockFn, [{}, { modelId: "grok-4.6" }]),
  stockSentinel,
  "models[slug] provider=native must return the stock provider",
);
var m3 = rtModels.wrapSession(stockFn, [{}, { modelId: "sand-default" }]);
assert.equal(
  m3.getModelId(),
  "gemini-3.7-flash-high",
  "unmapped slug must fall back to agents['*']",
);

// 5. agent UUID binding still beats the models map (most specific wins).
fs2.writeFileSync(
  tmpB,
  JSON.stringify({
    agents: {
      "2b030fcf-efe6-4115-9078-01b009a2379f": {
        modelId: "claude-opus-x",
        provider: "custom",
        hopBaseUrl: "http://127.0.0.1:9/v1",
      },
      "*": {
        modelId: "gemini-3.7-flash-high",
        provider: "custom",
        hopBaseUrl: "http://127.0.0.1:9/v1",
      },
    },
    models: {
      "grok-4.5-high": {
        modelId: "glm-5.3-flash",
        provider: "custom",
        hopBaseUrl: "http://127.0.0.1:9/v1",
      },
    },
  }),
);
var rtPrec = rerequire(tmpB);
var m4 = rtPrec.wrapSession(stockFn, [
  { agentId: "2b030fcf-efe6-4115-9078-01b009a2379f" },
  { modelId: "grok-4.5-high" },
]);
assert.equal(
  m4.getModelId(),
  "claude-opus-x",
  "agent UUID binding must beat models[slug]",
);

// 6. empty config entirely -> native passthrough, never a thrown turn.
fs2.writeFileSync(tmpB, JSON.stringify({ agents: {}, models: {} }));
var rtEmpty = rerequire(tmpB);
assert.equal(
  rtEmpty.wrapSession(stockFn, [{}, { modelId: "grok-4.5-high" }]),
  stockSentinel,
  "empty config must degrade to native",
);
fs2.unlinkSync(tmpB);

console.log("opengrok-runtime: ok");
