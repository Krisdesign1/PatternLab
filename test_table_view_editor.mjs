// Run with: node --test test_table_view_editor.mjs (no browser or packages).
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("./table_view_editor.js", import.meta.url), "utf8");
const { default: render, normalizeView, sortableValue, applyRichView } = await import(
  `data:text/javascript;base64,${Buffer.from(`${source}\nexport { sortableValue, applyRichView };`).toString("base64")}`
);

class Node {
  constructor(tag, document) {
    Object.assign(this, { tagName: tag.toUpperCase(), ownerDocument: document, children: [], style: {}, attrs: {}, className: "", _text: "", value: "" });
  }
  appendChild(node) {
    if (node.tagName === "#FRAGMENT") { [...node.children].forEach((child) => this.appendChild(child)); return node; }
    node.remove(); node.parentElement = this; this.children.push(node); return node;
  }
  append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
  remove() {
    if (this.parentElement) { const parent = this.parentElement; parent.children.splice(parent.children.indexOf(this), 1); this.parentElement = null; }
  }
  replaceChildren(...nodes) { this.children.forEach((node) => { node.parentElement = null; }); this.children = []; this.append(...nodes); }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(""); }
  // The editor tests mount no rich HTML. Rich table behavior is tested directly.
  set innerHTML(value) { assert.equal(value, ""); this.replaceChildren(); }
  get lastChild() { return this.children.at(-1); }
  setAttribute(key, value) { this.attrs[key] = value; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  get valueAsNumber() { return this.value === "" ? NaN : Number(this.value); }
  reportValidity() {
    return this.disabled || (this.value === "" ? !this.required
      : Number.isInteger(this.valueAsNumber) && this.valueAsNumber >= Number(this.min ?? 40) && this.valueAsNumber <= Number(this.max ?? 2000));
  }
}

function environment(initial = {}) {
  const storage = new Map(Object.entries(initial));
  const frames = new Map();
  const timers = new Map();
  const listeners = new Map();
  let sequence = 0;
  const window = {
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    requestAnimationFrame: (callback) => { frames.set(++sequence, callback); return sequence; },
    cancelAnimationFrame: (id) => frames.delete(id),
    setTimeout: (callback, delay) => { timers.set(++sequence, { callback, delay }); return sequence; },
    clearTimeout: (id) => timers.delete(id),
    addEventListener: (name, callback) => { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); },
    removeEventListener: (name, callback) => listeners.get(name)?.delete(callback),
  };
  const document = { defaultView: window, createElement: (tag) => new Node(tag, document), createDocumentFragment: () => new Node("#fragment", document) };
  return {
    parent: new Node("div", document), document, window, storage, frames, timers,
    flushFrames() { for (const [id, callback] of [...frames]) { frames.delete(id); callback(); } },
    runTimer() { const entry = timers.entries().next().value; if (!entry) return false; const [id, { callback }] = entry; timers.delete(id); callback(); return true; },
    dispatchStorage(event) { [...(listeners.get("storage") ?? [])].forEach((callback) => callback(event)); },
  };
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) { const found = find(child, predicate); if (found) return found; }
}
const control = (environment, label) => find(environment.parent, (node) => node.attrs["aria-label"] === label);
const click = (environment, label) => find(environment.parent, (node) => node.tagName === "BUTTON" && node.textContent === label).onclick();
const defaults = { version: 1, columns: ["a", "b"], hidden: [], widths: {}, sort: [], height: null };
const saved = { version: 1, columns: ["b", "a"], hidden: ["a"], widths: { b: 160 }, sort: [{ column: "b", direction: "desc" }], height: null };
const data = { storage_key: "first", columns: [{ id: "a", label: "A" }, { id: "b", label: "B" }], defaults, current: defaults, reset_token: null };
const mount = (environment, events, overrides = {}) => render({ data: { ...data, ...overrides }, parentElement: environment.parent, setStateValue: (...args) => events.push(args) });

test("schema reconciliation rejects invalid preferences and preserves new hidden defaults", () => {
  const columns = [...data.columns, { id: "c" }];
  const base = { ...defaults, columns: ["a", "b", "c"], hidden: ["c"] };
  const value = normalizeView({ ...saved, columns: ["b", "a", "removed", "b"], widths: { a: 39, b: 2000, c: 40.5 }, sort: [...saved.sort, ...saved.sort] }, columns, base);
  assert.deepEqual(value, { ...saved, columns: ["b", "a", "c"], hidden: ["a", "c"], widths: { b: 2000 } });
  assert.deepEqual(normalizeView({ version: 2 }, columns, base), base);
  const hostile = normalizeView(JSON.parse('{"version":1,"columns":["__proto__"],"widths":{"__proto__":160}}'), [{ id: "__proto__" }]);
  assert.equal(Object.getPrototypeOf(hostile.widths), Object.prototype);
  assert.equal(Object.hasOwn(hostile.widths, "__proto__"), true);
  assert.equal(hostile.widths.__proto__, 160);
});

test("initial restore retries a lost fragment event and stops immediately at ACK", () => {
  const env = environment({ first: JSON.stringify(saved) }), events = [];
  mount(env, events);
  assert.equal(events.length, 0);
  env.flushFrames();
  assert.deepEqual(events, [["view", saved]]);
  assert.equal(env.runTimer(), true);
  assert.equal(events.length, 2);
  mount(env, events, { current: saved });
  assert.equal(env.timers.size, 0);
  assert.equal(env.frames.size, 0);
});

test("unacknowledged restoration is strictly bounded to five deliveries", () => {
  const env = environment({ first: JSON.stringify(saved) }), events = [];
  mount(env, events); env.flushFrames();
  let count = 0;
  while (env.runTimer()) { count += 1; assert.ok(count < 10); }
  assert.equal(events.length, 5);
  mount(env, events);
  assert.equal(env.timers.size, 0);
  assert.equal(env.frames.size, 0);
});

test("cleanup cancels frames and timers, and remount resumes the remaining budget", () => {
  const env = environment({ first: JSON.stringify(saved) }), events = [];
  mount(env, events)(); env.flushFrames();
  assert.equal(events.length, 0);
  const cleanup = mount(env, events); env.flushFrames();
  assert.equal(events.length, 1); cleanup();
  assert.equal(env.timers.size, 0);
  mount(env, events); env.runTimer();
  assert.equal(events.length, 2);
});

test("changing table identity cancels the removed instance's pending restore", () => {
  const env = environment({ first: JSON.stringify(saved) }), events = [];
  mount(env, events);
  mount(env, events, { storage_key: "second" });
  env.flushFrames();
  assert.deepEqual(events, []);
  assert.equal(env.timers.size, 0);
});

test("draft edits wait for save and survive an ACK; reset removes storage", () => {
  const env = environment({ first: JSON.stringify(saved) }), events = [];
  mount(env, events); env.flushFrames();
  const visible = control(env, "Afficher A"); visible.checked = true; visible.onchange();
  assert.equal(events.length, 1);
  mount(env, events, { current: saved });
  assert.equal(control(env, "Afficher A").checked, true);
  click(env, "Enregistrer ma vue");
  const applied = { ...saved, hidden: [] };
  assert.deepEqual(JSON.parse(env.storage.get("first")), applied);
  assert.deepEqual(Object.keys(JSON.parse(env.storage.get("first"))).sort(), ["columns", "height", "hidden", "sort", "version", "widths"]);
  // A delayed ACK of the preceding intent must not overwrite the newer view.
  mount(env, events, { current: saved });
  assert.equal(control(env, "Afficher A").checked, true);
  mount(env, events, { current: applied });
  click(env, "Réinitialiser la vue");
  assert.equal(env.storage.has("first"), false);
  assert.deepEqual(events.at(-1), ["view", defaults]);
  mount(env, events, { current: defaults });
  assert.equal(env.timers.size, 0);
});

test("blocked browser storage keeps session application and accurate status", () => {
  const env = environment(), events = [];
  env.window.localStorage.getItem = () => { throw Error("blocked"); };
  env.window.localStorage.setItem = () => { throw Error("blocked"); };
  mount(env, events);
  const visible = control(env, "Afficher A"); visible.checked = false; visible.onchange();
  click(env, "Enregistrer ma vue");
  assert.deepEqual(events.at(-1)[1].hidden, ["a"]);
  assert.equal(find(env.parent, (node) => node.className === "tve-status").textContent, "Appliquée pour cette session ; stockage du navigateur indisponible.");
  assert.equal(env.storage.size, 0);
});

test("rich numeric sorting ignores metadata and keeps precise probabilities", () => {
  const cell = (text, primary = null) => ({ textContent: text, getAttribute: () => null, querySelector: (selector) => selector === "[data-sort]" ? null : primary });
  assert.equal(sortableValue(cell("1 234,56 %")).number, 1234.56);
  assert.equal(sortableValue(cell("1,23 %Choix 2", { textContent: "1,23 %", getAttribute: () => null })).number, 1.23);
  assert.equal(sortableValue(cell("—")).empty, true);
  assert.equal(sortableValue(cell("0,00 %", { textContent: "0,00 %", getAttribute: () => "1e-7 %" })).number, 1e-7);
});

test("rich table reorders cells, sorts stably, hides and restores original styles", () => {
  const env = environment(), body = new Node("tbody", env.document);
  const rows = [["first", "2"], ["second", "1"], ["third", "1"]].map((values, index) => {
    const node = new Node("tr", env.document);
    const cells = values.map((value) => { const cell = new Node("td", env.document); cell.textContent = value; node.appendChild(cell); return { node: cell, display: "", width: "", minWidth: "", maxWidth: "" }; });
    body.appendChild(node);
    return { node, cells, index };
  });
  const state = { schema: data.columns, applied: saved, richTables: [{ rows, groups: [{ body, rows }] }] };
  applyRichView(state);
  assert.deepEqual(body.children.map((row) => row.children[1].textContent), ["first", "second", "third"]);
  assert.equal(rows[0].cells[0].node.style.display, "none");
  assert.equal(rows[0].cells[1].node.style.width, "160px");
  state.applied = { ...saved, sort: [{ column: "b", direction: "asc" }] }; applyRichView(state);
  assert.deepEqual(body.children.map((row) => row.children[1].textContent), ["second", "third", "first"]);
  state.applied = defaults; applyRichView(state);
  assert.deepEqual(body.children.map((row) => row.children[0].textContent), ["first", "second", "third"]);
  assert.equal(rows[0].cells[0].node.style.display, "");
  assert.equal(rows[0].cells[1].node.style.width, "");
});

test("height migration preserves old columns and rejects malformed dimensions", () => {
  const legacy = { ...saved }; delete legacy.height;
  assert.deepEqual(normalizeView(legacy, data.columns, defaults), saved);
  for (const height of [null, "content", 200, 1100, 5000]) {
    assert.equal(normalizeView({ ...saved, height }, data.columns, defaults).height, height);
  }
  for (const height of [undefined, false, true, 199, 5001, -1, 800.5, NaN, Infinity, "900", "auto", {}, []]) {
    assert.equal(normalizeView({ ...saved, height }, data.columns, { ...defaults, height: 900 }).height, 900);
  }
  assert.equal(normalizeView({ ...saved, height: null }, data.columns, { ...defaults, height: 900 }).height, null);
});

test("custom height saves independently and restores on a new mount without losing columns", () => {
  const env = environment({ first: JSON.stringify(saved) }), events = [];
  mount(env, events); env.flushFrames();
  const mode = control(env, "Hauteur du tableau");
  const input = control(env, "Hauteur du tableau, en pixels");
  assert.equal(mode.value, "default"); assert.equal(input.disabled, true);
  mode.value = "custom"; mode.onchange();
  assert.equal(input.value, "500"); assert.equal(input.disabled, false);
  input.value = "2450"; input.oninput();
  assert.equal(events.length, 1);
  click(env, "Enregistrer ma vue");
  const taller = { ...saved, height: 2450 };
  assert.deepEqual(events.at(-1), ["view", taller]);
  assert.deepEqual(JSON.parse(env.storage.get("first")), taller);
  const reopened = environment({ first: env.storage.get("first") }), restored = [];
  mount(reopened, restored); reopened.flushFrames();
  assert.equal(control(reopened, "Hauteur du tableau").value, "custom");
  assert.equal(control(reopened, "Hauteur du tableau, en pixels").value, "2450");
  assert.deepEqual(restored.at(-1), ["view", taller]);
  mount(reopened, restored, { current: taller });
  assert.equal(reopened.timers.size, 0);
  click(reopened, "Réinitialiser la vue");
  assert.equal(control(reopened, "Hauteur du tableau").value, "default");
  assert.deepEqual(restored.at(-1), ["view", defaults]);
  assert.equal(reopened.storage.has("first"), false);
});

test("invalid height cannot save; adapting to loaded rows preserves pagination-independent preferences", () => {
  const env = environment(), events = [];
  mount(env, events);
  const mode = control(env, "Hauteur du tableau"), input = control(env, "Hauteur du tableau, en pixels");
  mode.value = "custom"; mode.onchange();
  for (const bad of ["", "199", "5001", "500.5", "not-a-number"]) {
    input.value = bad; input.oninput();
    click(env, "Enregistrer ma vue");
    assert.equal(env.storage.size, 0); assert.equal(events.length, 0);
  }
  mode.value = "content"; mode.onchange();
  assert.equal(input.disabled, true);
  click(env, "Enregistrer ma vue");
  assert.deepEqual(events.at(-1), ["view", { ...defaults, height: "content" }]);
  assert.deepEqual(JSON.parse(env.storage.get("first")), { ...defaults, height: "content" });
  env.dispatchStorage({ key: "first", newValue: JSON.stringify({ ...saved, height: 1200 }) });
  assert.equal(mode.value, "custom"); assert.equal(input.value, "1200");
  assert.deepEqual(events.at(-1), ["view", { ...saved, height: 1200 }]);
});

test("rich table height changes only its owned scrolling container and restores natural height", () => {
  const env = environment();
  const state = { schema: data.columns, applied: { ...defaults, height: 1000 }, richTables: [], rich: new Node("div", env.document) };
  applyRichView(state);
  assert.equal(state.rich.style.maxHeight, "1000px"); assert.equal(state.rich.style.overflowY, "auto");
  state.applied = { ...defaults, height: "content" }; applyRichView(state);
  assert.equal(state.rich.style.maxHeight, ""); assert.equal(state.rich.style.overflowY, "");
  state.applied = defaults; applyRichView(state);
  assert.equal(state.rich.style.maxHeight, "");
});
