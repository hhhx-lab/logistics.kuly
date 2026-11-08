import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

// 直接执行真实订阅器，替换浏览器连接与API；不连接业务数据库或模型。
async function harness({ pages = [] } = {}) {
  const sockets = [], timers = new Map(), requests = [], connections = [];
  let timerId = 0;
  class FakeEventSource {
    static OPEN = 1;
    readyState = 0;
    listeners = new Map();
    closed = false;
    constructor(url) { this.url = url; sockets.push(this); }
    addEventListener(type, listener) { this.listeners.set(type, listener); }
    close() { this.closed = true; this.readyState = 2; }
    emit(type, seq, payload = {}) {
      this.listeners.get(type)?.({ lastEventId: String(seq), data: JSON.stringify({ seq, occurred_at: "2026-10-27T02:48:11Z", ...payload }) });
    }
    fail() { this.onerror?.(); }
  }
  class ApiError extends Error {}
  const context = vm.createContext({
    EventSource: FakeEventSource, AbortController, Date,
    setTimeout: (callback) => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  });
  const source = await readFile(new URL("../src/api/events.ts", import.meta.url), "utf8");
  const module = new vm.SourceTextModule(ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  }).outputText, { context });
  await module.link(async (specifier) => {
    const exports = specifier === "react" ? { useEffect() {}, useRef() {}, useState() {} }
      : specifier === "@tanstack/react-query" ? { useQueryClient() {} }
        : { ApiError, API_PREFIX: "/api/v1", api: async (path, options) => {
          requests.push({ path, options });
          assert.ok(pages.length, "Unexpected additional event request");
          return pages.shift();
        } };
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  const subscribe = (options) => module.namespace.subscribeSequenced({
    kind: "turn", path: "/conversations/C/turns/T/events/stream", afterSeq: 50,
    onConnection: state => connections.push(state), onGap: () => assert.fail("Unexpected event gap"),
    ...options,
  });
  const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
  const runPoll = async () => {
    const entry = timers.entries().next().value;
    assert.ok(entry, "Expected a recovery poll");
    timers.delete(entry[0]); entry[1](); await settle();
  };
  return { subscribe, sockets, timers, requests, connections, settle, runPoll };
}

test("正式答复已终态时关闭回合流，不再按最后seq重连", async () => {
  const h = await harness();
  const events = [];
  const handle = h.subscribe({ onEvent: event => events.push(event.type), isTerminal: async () => true });
  h.sockets[0].emit("message_final", 51, { status: "completed" });
  await h.settle();
  assert.deepEqual(events, ["message_final"]);
  assert.equal(h.sockets[0].closed, true);
  assert.equal(h.connections.at(-1), "closed");
  h.sockets[0].fail();
  assert.equal(h.timers.size, 0);
  assert.equal(h.requests.length, 0);
  handle.close();
});

test("首条正式答复后仍有续答时保留订阅，续答终态后关闭", async () => {
  const h = await harness();
  let done = false;
  const events = [];
  const handle = h.subscribe({ onEvent: event => events.push(event.type), isTerminal: async () => done });
  h.sockets[0].emit("message_final", 51, { status: "completed" });
  h.sockets[0].emit("continuation_pending", 52, { status: "pending" });
  await h.settle();
  assert.equal(h.sockets[0].closed, false);
  done = true;
  h.sockets[0].emit("continuation_final", 53, { status: "completed" });
  await h.settle();
  assert.deepEqual(events, ["message_final", "continuation_pending", "continuation_final"]);
  assert.equal(h.sockets[0].closed, true);
  handle.close();
});

test("终态连接断开后的空事件页核对状态并停止轮询", async () => {
  const h = await harness({ pages: [{ items: [], last_seq: 51 }] });
  const handle = h.subscribe({ afterSeq: 51, onEvent: () => assert.fail("Unexpected event"), isTerminal: async () => true });
  h.sockets[0].fail();
  await h.runPoll();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.query.after_seq, 51);
  assert.equal(h.sockets.length, 1);
  assert.equal(h.timers.size, 0);
  assert.equal(h.connections.at(-1), "closed");
  handle.close();
});

test("非终态空事件页仍能从持久seq恢复连接", async () => {
  const h = await harness({ pages: [{ items: [], last_seq: 50 }] });
  const handle = h.subscribe({ onEvent: () => {}, isTerminal: async () => false });
  h.sockets[0].fail();
  await h.runPoll();
  assert.equal(h.sockets.length, 2);
  assert.ok(h.sockets[1].url.endsWith("after_seq=50"));
  assert.equal(h.sockets[1].closed, false);
  handle.close();
});
