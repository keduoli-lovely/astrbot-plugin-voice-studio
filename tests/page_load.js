/**
 * Load views/studio/app.js in a minimal fake DOM to catch startup crashes
 * (e.g. accessing state.workerModels before it exists). Run with:
 *   node tests/page_load.js
 */
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appPath = path.join(__dirname, "..", "views", "studio", "app.js");
const source = fs.readFileSync(appPath, "utf8");

const elements = {};
function makeElement(tag) {
  return {
    tagName: tag,
    id: "",
    value: "",
    textContent: "",
    className: "",
    type: "",
    placeholder: "",
    disabled: false,
    maxLength: 0,
    selectedIndex: 0,
    options: [],
    style: {},
    dataset: {},
    children: [],
    appendChild(child) {
      this.children.push(child);
      if (child && child.id) elements[child.id] = child;
      return child;
    },
    replaceChildren(...kids) {
      this.children = kids;
    },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    remove() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
  };
}

const settings = {
  ttsProvider: "indextts",
  providers: {},
  worker: {},
  defaultVoice: "",
  defaultEmotion: "auto",
  emotionIntensity: 0.6,
  ttsSpeed: 1.0,
};

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  Promise,
  JSON,
  Math,
  Date,
  Object,
  Array,
  String,
  Number,
  Boolean,
  Error,
  document: {
    getElementById(id) {
      if (!elements[id]) {
        elements[id] = makeElement("div");
        elements[id].id = id;
      }
      return elements[id];
    },
    createElement(tag) {
      return makeElement(tag);
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    documentElement: makeElement("html"),
    body: makeElement("body"),
  },
  window: {
    AstrBotPluginView: {
      ready: async () => ({ displayName: "Tester" }),
      apiGet: async (endpoint) => {
        if (endpoint === "settings") return { settings, providers: {}, dataDir: "/tmp", voiceDir: "/tmp" };
        if (endpoint === "voices") return { voices: [], defaultVoice: "" };
        if (endpoint === "worker/health") return { ok: true, health: { ok: true } };
        if (endpoint === "worker/models") return { ok: true, models: [{ id: "m.pth", name: "m" }], indexes: [] };
        return {};
      },
      apiPost: async () => ({ ok: true }),
      upload: async () => ({ ok: true }),
    },
  },
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

const context = vm.createContext(sandbox);
try {
  vm.runInContext(source, context, { filename: "views/studio/app.js" });
} catch (error) {
  console.error("page load FAILED:", error && error.stack ? error.stack : error);
  process.exit(1);
}
setTimeout(() => {
  console.log("page load OK");
  process.exit(0);
}, 600);
