"use strict";
var http = require("http");
var assert = require("assert");
var hop = require("./openai-hop-session.cjs");

function parseBody(chunks) {
  var raw = Buffer.concat(chunks).toString("utf8");
  try {
    return JSON.parse(raw);
  } catch (err) {
    console.error("test server got a non-JSON body: " + raw.slice(0, 200));
    throw err;
  }
}

assert.equal(
  hop.completionsUrl("http://127.0.0.1:18790/v1"),
  "http://127.0.0.1:18790/v1/chat/completions",
);
assert.equal(
  hop.completionsUrl("http://127.0.0.1:18790/v1/"),
  "http://127.0.0.1:18790/v1/chat/completions",
);
assert.equal(
  hop.completionsUrl("http://127.0.0.1:18790/v1/chat/completions"),
  "http://127.0.0.1:18790/v1/chat/completions",
);

var session = hop.createOpenAiHopSession({
  modelId: "glm-5.3-flash",
  baseUrl: "http://127.0.0.1:9/v1",
  parameters: [{ id: "fast", value: "true" }],
});
assert.equal(session.opengrok, true);
assert.equal(session.modelId, "glm-5.3-flash");
assert.equal(session.getThinkingDetails(), undefined);
session.abort();
session.runTurn({ messages: [{ role: "user", content: "x" }] }).then(
  function () {
    console.error("aborted session should reject");
    process.exit(1);
  },
  function () {
    liveServer();
  },
);

function liveServer() {
  var sawContentType = false;
  var sawThinkingOff = false;
  var server = http.createServer(function (req, res) {
    var chunks = [];
    req.on("data", function (c) {
      chunks.push(c);
    });
    req.on("end", function () {
      sawContentType = /application\/json/i.test(
        req.headers["content-type"] || "",
      );
      var body = parseBody(chunks);
      sawThinkingOff = body.thinking && body.thinking.type === "disabled";
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          id: "test",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "OCG_OK" },
              finish_reason: "stop",
            },
          ],
        }),
      );
    });
  });
  server.listen(0, "127.0.0.1", function () {
    var port = server.address().port;
    var s = hop.createOpenAiHopSession({
      modelId: "glm-5.3-flash",
      baseUrl: "http://127.0.0.1:" + port + "/v1",
      parameters: [{ id: "fast", value: "true" }],
    });
    s.runTurn({ messages: [{ role: "user", content: "ping" }] })
      .then(function (out) {
        assert.equal(out.content, "OCG_OK");
        assert.equal(
          sawContentType,
          true,
          "Content-Type must be set (empty 200 otherwise)",
        );
        assert.equal(
          sawThinkingOff,
          true,
          "GLM fast should set thinking.disabled",
        );
        server.close();
        abortRecoverTest();
      })
      .catch(function (err) {
        server.close();
        console.error(err);
        process.exit(1);
      });
  });
}

function abortRecoverTest() {
  var server = http.createServer(function (req, res) {
    var chunks = [];
    req.on("data", function (c) {
      chunks.push(c);
    });
    req.on("end", function () {
      setTimeout(function () {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            id: "recover",
            choices: [
              {
                index: 0,
                message: { role: "assistant", content: "RECOVER_OK" },
                finish_reason: "stop",
              },
            ],
          }),
        );
      }, 30);
    });
  });
  server.listen(0, "127.0.0.1", function () {
    var port = server.address().port;
    var s = hop.createOpenAiHopSession({
      modelId: "glm-5.3-flash",
      baseUrl: "http://127.0.0.1:" + port + "/v1",
    });
    var inflight = s.runTurn({ messages: [{ role: "user", content: "slow" }] });
    s.abort();
    inflight
      .then(
        function () {
          server.close();
          console.error("in-flight abort should reject");
          process.exit(1);
        },
        function (err) {
          assert.ok(/aborted/.test(err.message));
          return s.runTurn({ messages: [{ role: "user", content: "again" }] });
        },
      )
      .then(function (out) {
        assert.equal(out.content, "RECOVER_OK");
        server.close();
        effortDefaultTest();
      })
      .catch(function (err) {
        server.close();
        console.error(err);
        process.exit(1);
      });
  });
}

function effortDefaultTest() {
  var seen = [];
  var server = http.createServer(function (req, res) {
    var chunks = [];
    req.on("data", function (c) {
      chunks.push(c);
    });
    req.on("end", function () {
      seen.push(parseBody(chunks));
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          id: "effort",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "EFFORT_OK" },
              finish_reason: "stop",
            },
          ],
        }),
      );
    });
  });
  server.listen(0, "127.0.0.1", function () {
    var base = "http://127.0.0.1:" + server.address().port + "/v1";
    var bare = hop.createOpenAiHopSession({
      modelId: "custom-slug",
      baseUrl: base,
    });
    var routed = hop.createOpenAiHopSession({
      modelId: "custom-slug",
      provider: "glm",
      baseUrl: base,
      parameters: [{ id: "fast", value: "true" }],
    });
    bare
      .runTurn({ messages: [{ role: "user", content: "a" }] })
      .then(function () {
        return routed.runTurn({ messages: [{ role: "user", content: "b" }] });
      })
      .then(function () {
        assert.equal(
          seen[0].reasoning_effort,
          "high",
          "a binding that names no effort is an own-channel binding: think deeply",
        );
        assert.deepEqual(
          seen[1].thinking,
          { type: "disabled" },
          "provider hint must reach the wire map even when the slug is unrecognizable",
        );
        assert.equal(
          seen[1].reasoning_effort,
          "low",
          "fast must still win over the high default",
        );
        server.close();
        console.log("openai-hop-session: ok");
      })
      .catch(function (err) {
        server.close();
        console.error(err);
        process.exit(1);
      });
  });
}
