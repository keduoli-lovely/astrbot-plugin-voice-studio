/* 语音工坊设置页（AstrBot 插件可视化视图）。
 *
 * 纯原生 JS，无第三方依赖；通过 window.AstrBotPluginView bridge 调用插件后端：
 *   apiGet("settings") / apiPost("settings", {settings}) / apiGet("voices")
 *   bridge.upload("voices/upload", file) / apiPost("voices/delete")
 *   apiPost("test", {text, provider, voice, emotion})
 *   apiGet("worker/health") / apiPost("worker/process", {process, action})
 */

"use strict";

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;

const FORMAT_OPTIONS = ["wav", "mp3", "opus", "flac", "aac", "pcm"];

const PROVIDER_FIELDS = {
  indextts: [
    { key: "baseUrl", label: "Gradio 地址", type: "text", placeholder: "http://127.0.0.1:7860" },
    {
      key: "lang",
      label: "语言",
      type: "select",
      options: [
        ["ZH", "中文"],
        ["EN", "English"],
        ["JA", "日本語"],
        ["AR", "العربية"],
        ["ES", "Español"],
      ],
    },
    { key: "emoWeight", label: "情感权重 (0~1)", type: "number", min: 0, max: 1, step: 0.05 },
    { key: "durationFactor", label: "时长系数", type: "number", min: 0.5, max: 2, step: 0.05 },
  ],
  gptsovits: [
    { key: "baseUrl", label: "api_v2 地址", type: "text", placeholder: "http://127.0.0.1:9880" },
    { key: "textLang", label: "文本语言", type: "text", placeholder: "zh" },
    { key: "promptText", label: "参考音频原文", type: "text", placeholder: "留空则 ref_free=true" },
    { key: "promptLang", label: "参考音频语言", type: "text", placeholder: "zh" },
    { key: "speed", label: "语速", type: "number", min: 0.5, max: 2, step: 0.05 },
  ],
  siliconflow: [
    { key: "baseUrl", label: "API Base URL", type: "text" },
    { key: "apiKey", label: "API Key", type: "password" },
    { key: "model", label: "模型", type: "text" },
    { key: "voice", label: "音色 id", type: "text" },
    { key: "format", label: "输出格式", type: "select", options: FORMAT_OPTIONS },
  ],
  minimax: [
    { key: "baseUrl", label: "API Base URL", type: "text" },
    { key: "apiKey", label: "API Key", type: "password" },
    { key: "groupId", label: "GroupId（可选）", type: "text" },
    { key: "model", label: "模型", type: "text" },
    { key: "voice", label: "voice_id", type: "text" },
    { key: "format", label: "输出格式", type: "select", options: FORMAT_OPTIONS },
  ],
  mimo: [
    { key: "baseUrl", label: "API Base URL", type: "text", placeholder: "按 MiMo 官方文档填写" },
    { key: "apiKey", label: "API Key", type: "password" },
    { key: "model", label: "模型", type: "text" },
    { key: "voice", label: "音色 id", type: "text" },
    { key: "style", label: "默认风格提示", type: "text" },
    { key: "format", label: "输出格式", type: "select", options: FORMAT_OPTIONS },
  ],
  openai: [
    { key: "baseUrl", label: "API Base URL", type: "text" },
    { key: "apiKey", label: "API Key", type: "password" },
    { key: "model", label: "模型", type: "text" },
    { key: "voice", label: "音色", type: "text" },
    { key: "format", label: "输出格式", type: "select", options: FORMAT_OPTIONS },
    {
      key: "emotionInstructions",
      label: "情绪写入 instructions",
      type: "select",
      options: [
        ["true", "启用"],
        ["false", "关闭"],
      ],
    },
  ],
};

const WORKER_FIELDS = [
  { key: "url", label: "Worker 地址", type: "text", placeholder: "http://127.0.0.1:7870" },
  { key: "token", label: "Token", type: "password" },
  { key: "model", label: "RVC 模型（来自 worker API）", type: "select", options: [] },
  { key: "index", label: "Index（来自 worker API，可选）", type: "select", options: [] },
  {
    key: "f0Method",
    label: "F0 方法",
    type: "select",
    options: ["rmvpe", "pm", "harvest", "crepe", "fcpe"],
  },
  { key: "keyShift", label: "变调（半音）", type: "number", min: -24, max: 24, step: 1 },
];

const EMOTIONS = [
  ["auto", "自动"],
  ["neutral", "中性"],
  ["happy", "开心"],
  ["angry", "生气"],
  ["sad", "悲伤"],
  ["fear", "害怕"],
  ["disgust", "厌恶"],
  ["down", "低落"],
  ["surprise", "惊喜"],
  ["calm", "平静"],
];

const state = {
  settings: {
    ttsProvider: "indextts",
    providers: {},
    worker: {},
    defaultVoice: "",
    defaultEmotion: "auto",
    emotionIntensity: 0.6,
    ttsSpeed: 1.0,
  },
  providers: {},
  workerModels: { models: [], indexes: [] },
  voices: [],
};

function $(id) {
  return document.getElementById(id);
}

function make(tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent !== undefined && textContent !== null) node.textContent = String(textContent);
  return node;
}

function setStatus(id, message, kind) {
  const node = $(id);
  if (!node) return;
  node.textContent = message || "";
  node.className = "status" + (kind ? " " + kind : "");
}

function formatBytes(value) {
  const size = Number(value) || 0;
  if (size < 1024) return size + "B";
  if (size < 1024 * 1024) return (size / 1024).toFixed(1) + "KB";
  return (size / 1024 / 1024).toFixed(2) + "MB";
}

function errorText(error) {
  if (!error) return "未知错误";
  if (typeof error === "string") return error;
  return error.message || String(error);
}

function fillSelect(select, options, value) {
  select.replaceChildren();
  options.forEach(function (option) {
    const item = Array.isArray(option) ? option : [option, option];
    const node = document.createElement("option");
    node.value = String(item[0]);
    node.textContent = String(item[1]);
    select.appendChild(node);
  });
  if (value !== undefined && value !== null) {
    const wanted = String(value);
    let found = false;
    for (let index = 0; index < select.options.length; index += 1) {
      if (select.options[index].value === wanted) {
        select.selectedIndex = index;
        found = true;
        break;
      }
    }
    if (!found) {
      const node = document.createElement("option");
      node.value = wanted;
      node.textContent = wanted;
      select.appendChild(node);
      select.value = wanted;
    }
  }
}

function buildField(provider, spec, value, idPrefix) {
  const field = make("label", "field");
  field.appendChild(make("span", null, spec.label));
  let control;
  if (spec.type === "select") {
    control = document.createElement("select");
    fillSelect(control, spec.options || [], value);
  } else {
    control = document.createElement("input");
    control.type = spec.type || "text";
    if (spec.placeholder) control.placeholder = spec.placeholder;
    if (spec.min !== undefined) control.min = String(spec.min);
    if (spec.max !== undefined) control.max = String(spec.max);
    if (spec.step !== undefined) control.step = String(spec.step);
    if (spec.type === "password") control.autocomplete = "new-password";
    control.value = value === undefined || value === null ? "" : String(value);
  }
  control.id = idPrefix + "-" + spec.key;
  control.dataset.provider = provider;
  control.dataset.key = spec.key;
  field.appendChild(control);
  return field;
}

function providerValues(provider) {
  if (!state.settings.providers[provider]) state.settings.providers[provider] = {};
  return state.settings.providers[provider];
}

function bindProviderInput(control, provider, spec) {
  const handler = function () {
    const target = providerValues(provider);
    if (spec.type === "number") {
      const number = Number(control.value);
      target[spec.key] = Number.isFinite(number) ? number : control.value;
    } else if (spec.type === "select" && spec.options === FORMAT_OPTIONS) {
      target[spec.key] = control.value;
    } else if (spec.key === "emotionInstructions") {
      target[spec.key] = control.value === "true";
    } else {
      target[spec.key] = control.value;
    }
  };
  control.addEventListener("input", handler);
  control.addEventListener("change", handler);
}

function renderProviderSelect() {
  const select = $("provider-select");
  const options = Object.keys(state.providers).map(function (providerId) {
    return [providerId, state.providers[providerId].label || providerId];
  });
  fillSelect(select, options, state.settings.ttsProvider);
}

function renderProviderFields() {
  const container = $("provider-fields");
  const provider = $("provider-select").value;
  container.replaceChildren();
  const fields = PROVIDER_FIELDS[provider] || [];
  if (!fields.length) {
    container.appendChild(make("div", "empty", "该提供商没有可配置项"));
    return;
  }
  const values = providerValues(provider);
  fields.forEach(function (spec) {
    const control = buildField(provider, spec, values[spec.key], "field-" + provider);
    bindProviderInput(control.querySelector("input,select"), provider, spec);
    container.appendChild(control);
  });
}

function fillCommonFields() {
  fillSelect($("default-emotion"), EMOTIONS, state.settings.defaultEmotion || "auto");
  $("emotion-intensity").value = String(state.settings.emotionIntensity ?? 0.6);
  $("tts-speed").value = String(state.settings.ttsSpeed ?? 1.0);
  $("default-voice").value = state.settings.defaultVoice || "";
}

function workerFieldOptions(spec) {
  if (spec.key === "model") {
    return (state.workerModels.models || []).map(function (item) {
      return [String(item.id), item.name ? item.name + "（" + item.id + "）" : String(item.id)];
    });
  }
  if (spec.key === "index") {
    return [["", "（自动匹配）"]].concat(
      (state.workerModels.indexes || []).map(function (item) {
        return [String(item.id), item.name || String(item.id)];
      })
    );
  }
  return spec.options || [];
}

function renderWorkerFields() {
  const container = $("worker-fields");
  container.replaceChildren();
  const values = state.settings.worker || {};
  WORKER_FIELDS.forEach(function (raw) {
    const spec = Object.assign({}, raw);
    if (spec.key === "model" || spec.key === "index") {
      spec.options = workerFieldOptions(spec);
    }
    const control = buildField("worker", spec, values[spec.key], "worker");
    container.appendChild(control);
  });
}

async function refreshWorkerModels() {
  const button = $("worker-refresh-models");
  if (button) button.disabled = true;
  setStatus("worker-models-status", "获取中…");
  try {
    const result = await bridge.apiGet("worker/models");
    state.workerModels = {
      models: (result && result.models) || [],
      indexes: (result && result.indexes) || [],
    };
    renderWorkerFields();
    setStatus(
      "worker-models-status",
      "已从 worker 获取 " + state.workerModels.models.length + " 个模型、" +
        state.workerModels.indexes.length + " 个索引",
      "ok"
    );
  } catch (error) {
    setStatus("worker-models-status", "翻唱 worker 获取模型失败：" + errorText(error), "err");
  } finally {
    if (button) button.disabled = false;
  }
}

function readWorkerFields() {
  const result = {};
  WORKER_FIELDS.forEach(function (spec) {
    const node = $("worker-" + spec.key);
    if (!node) return;
    if (spec.type === "number") {
      const number = Number(node.value);
      result[spec.key] = Number.isFinite(number) ? Math.round(number) : 0;
    } else {
      result[spec.key] = node.value.trim();
    }
  });
  return result;
}

function renderVoices() {
  const container = $("voice-list");
  container.replaceChildren();
  if (!state.voices.length) {
    container.appendChild(
      make("div", "empty", "音色库是空的：上传一个参考音频，或从服务器路径导入。")
    );
    return;
  }
  state.voices.forEach(function (voice) {
    const item = make("div", "voice-item");
    item.appendChild(make("span", "voice-name", voice.name || voice.id));
    item.appendChild(
      make("span", "voice-meta", "id: " + voice.id + " · " + formatBytes(voice.size))
    );
    if (state.settings.defaultVoice && state.settings.defaultVoice === voice.id) {
      item.appendChild(make("span", "badge", "默认"));
    }
    if (!voice.available) item.appendChild(make("span", "badge missing", "文件缺失"));
    item.appendChild(make("span", "spacer"));

    const defaultButton = make("button", "btn ghost", "设为默认");
    defaultButton.type = "button";
    defaultButton.addEventListener("click", function () {
      setDefaultVoice(voice.id);
    });
    item.appendChild(defaultButton);

    const renameButton = make("button", "btn ghost", "重命名");
    renameButton.type = "button";
    renameButton.addEventListener("click", function () {
      renameVoice(voice, item);
    });
    item.appendChild(renameButton);

    const deleteButton = make("button", "btn danger", "删除");
    deleteButton.type = "button";
    deleteButton.addEventListener("click", function () {
      deleteVoice(voice);
    });
    item.appendChild(deleteButton);
    container.appendChild(item);
  });
}

function renameVoice(voice, item) {
  const input = make("input", "grow");
  input.value = voice.name || voice.id;
  input.maxLength = 60;
  const save = make("button", "btn primary", "保存");
  const cancel = make("button", "btn ghost", "取消");
  item.replaceChildren(input, save, cancel);
  cancel.addEventListener("click", function () {
    renderVoices();
  });
  save.addEventListener("click", async function () {
    const name = input.value.trim();
    if (!name) {
      setStatus("voice-status", "音色名称不能为空", "err");
      return;
    }
    try {
      await bridge.apiPost("voices/rename", { id: voice.id, newName: name });
      setStatus("voice-status", "已重命名为 " + name, "ok");
      await refreshVoices();
    } catch (error) {
      setStatus("voice-status", "重命名失败：" + errorText(error), "err");
    }
  });
}

async function testTts() {
  const button = $("tts-test");
  button.disabled = true;
  setStatus("tts-status", "测试中…");
  try {
    const provider = $("provider-select").value;
    const result = await bridge.apiGet("tts/health", { provider: provider });
    if (result && result.ok) {
      setStatus("tts-status", "语音合成连接成功：" + (result.detail || provider), "ok");
    } else {
      setStatus("tts-status", "语音合成连接失败：" + ((result && result.detail) || "未知错误"), "err");
    }
  } catch (error) {
    setStatus("tts-status", "语音合成连接失败：" + errorText(error), "err");
  } finally {
    if (button) button.disabled = false;
  }
}

function renderPreviewOptions() {
  fillSelect($("preview-emotion"), EMOTIONS, "auto");
  const provider = $("provider-select").value;
  const meta = state.providers[provider] || {};
  const options = [["", "（使用默认音色）"]];
  if (meta.needsRef) {
    state.voices
      .filter(function (voice) {
        return voice.available;
      })
      .forEach(function (voice) {
        options.push([voice.id, voice.name || voice.id]);
      });
  }
  fillSelect($("preview-voice"), options, "");
  $("preview-voice").disabled = !meta.needsRef;
}

async function refreshSettings() {
  const result = await bridge.apiGet("settings");
  if (result && result.settings) state.settings = result.settings;
  if (result && result.providers) state.providers = result.providers;
  renderProviderSelect();
  renderProviderFields();
  fillCommonFields();
  renderWorkerFields();
  renderPreviewOptions();
}

async function refreshVoices() {
  const result = await bridge.apiGet("voices");
  state.voices = (result && result.voices) || [];
  if (result && result.defaultVoice !== undefined) {
    state.settings.defaultVoice = result.defaultVoice;
  }
  $("default-voice").value = state.settings.defaultVoice || "";
  renderVoices();
  renderPreviewOptions();
}

async function saveSettings() {
  const button = $("save-settings");
  button.disabled = true;
  setStatus("save-status", "保存中…");
  try {
    const payload = {
      settings: {
        ttsProvider: $("provider-select").value,
        providers: state.settings.providers,
        worker: readWorkerFields(),
        defaultVoice: $("default-voice").value,
        defaultEmotion: $("default-emotion").value,
        emotionIntensity: Number($("emotion-intensity").value),
        ttsSpeed: Number($("tts-speed").value),
      },
    };
    const result = await bridge.apiPost("settings", payload);
    if (result && result.settings) state.settings = result.settings;
    setStatus("save-status", "已保存", "ok");
  } catch (error) {
    setStatus("save-status", "保存失败：" + errorText(error), "err");
  } finally {
    if (button) button.disabled = false;
  }
}

async function uploadVoice() {
  const input = $("voice-file");
  const file = input.files && input.files[0];
  if (!file) {
    setStatus("voice-status", "请先选择一个音频文件", "err");
    return;
  }
  const button = $("voice-upload");
  button.disabled = true;
  setStatus("voice-status", "上传中…");
  try {
    const result = await bridge.upload("voices/upload", file);
    const name = result && result.voice ? result.voice.name : "";
    setStatus("voice-status", name ? "已上传「" + name + "」" : "已上传", "ok");
    input.value = "";
    await refreshVoices();
  } catch (error) {
    setStatus("voice-status", "上传失败：" + errorText(error), "err");
  } finally {
    if (button) button.disabled = false;
  }
}

async function importVoice() {
  const path = $("import-path").value.trim();
  const name = $("import-name").value.trim();
  if (!path) {
    setStatus("voice-status", "请填写服务器上的音频绝对路径", "err");
    return;
  }
  const button = $("voice-import");
  button.disabled = true;
  setStatus("voice-status", "导入中…");
  try {
    const result = await bridge.apiPost("voices/import", { path: path, name: name });
    const voiceName = result && result.voice ? result.voice.name : "";
    setStatus("voice-status", "已导入「" + voiceName + "」", "ok");
    $("import-path").value = "";
    $("import-name").value = "";
    await refreshVoices();
  } catch (error) {
    setStatus("voice-status", "导入失败：" + errorText(error), "err");
  } finally {
    if (button) button.disabled = false;
  }
}

async function deleteVoice(voice) {
  const label = voice.name || voice.id;
  if (!window.confirm("确认从音色库删除「" + label + "」？此操作不可恢复。")) return;
  try {
    await bridge.apiPost("voices/delete", { id: voice.id });
    if (state.settings.defaultVoice === voice.id) {
      state.settings.defaultVoice = "";
      await bridge.apiPost("settings", { settings: { defaultVoice: "" } });
    }
    setStatus("voice-status", "已删除「" + label + "」", "ok");
    await refreshVoices();
  } catch (error) {
    setStatus("voice-status", "删除失败：" + errorText(error), "err");
  }
}

async function setDefaultVoice(voiceId) {
  try {
    await bridge.apiPost("settings", { settings: { defaultVoice: voiceId } });
    state.settings.defaultVoice = voiceId;
    $("default-voice").value = voiceId;
    renderVoices();
    setStatus("voice-status", "已设为默认音色", "ok");
  } catch (error) {
    setStatus("voice-status", "设置失败：" + errorText(error), "err");
  }
}

async function testWorker() {
  const button = $("worker-test");
  button.disabled = true;
  setStatus("worker-status", "测试中…");
  $("worker-output").textContent = "";
  try {
    const result = await bridge.apiGet("worker/health");
    $("worker-output").textContent = JSON.stringify(
      (result && result.health) || result || {},
      null,
      2
    );
    setStatus("worker-status", "连接成功", "ok");
  } catch (error) {
    setStatus("worker-status", "翻唱 worker 连接失败：" + errorText(error), "err");
  } finally {
    if (button) button.disabled = false;
  }
}

async function workerProcess(process, action) {
  setStatus("worker-status", "执行中…");
  try {
    const result = await bridge.apiPost("worker/process", { process: process, action: action });
    $("worker-output").textContent = JSON.stringify(
      (result && result.result) || result || {},
      null,
      2
    );
    setStatus("worker-status", "已执行：" + process + " " + action, "ok");
  } catch (error) {
    setStatus("worker-status", "操作失败：" + errorText(error), "err");
  }
}

function normalizeBridgeResult(value) {
  if (typeof value !== "string") return value;
  const raw = value;
  try {
    return JSON.parse(raw);
  } catch (error) {
    if (raw.length > 64 && /^[A-Za-z0-9+/=\s]+$/.test(raw)) {
      return { data: raw.replace(/\s+/g, ""), mime: "audio/mpeg" };
    }
    throw new Error("后端返回了非 JSON 字符串（前 120 字符）：" + raw.slice(0, 120));
  }
}

async function preview() {
  const text = $("preview-text").value.trim();
  if (text.length > 20) {
    setStatus("preview-status", "语音合成试听：文本请控制在 20 字以内", "err");
    return;
  }
  if (!text) {
    setStatus("preview-status", "请先填写试听文本", "err");
    return;
  }
  const button = $("preview-btn");
  button.disabled = true;
  setStatus("preview-status", "合成中…");
  try {
    const payload = {
      text: text,
      provider: $("provider-select").value,
      voice: $("preview-voice").value || null,
      emotion: $("preview-emotion").value || "auto",
    };
    let result = normalizeBridgeResult(await bridge.apiPost("test", payload));
    let audioData = "";
    if (result && typeof result.data === "string" && result.data) {
      audioData = result.data;
    } else if (result && result.previewId) {
      const total = Math.max(1, Number(result.chunks) || 1);
      const parts = [];
      for (let index = 0; index < total; index += 1) {
        const chunk = normalizeBridgeResult(
          await bridge.apiGet("test/chunk", { id: result.previewId, index: index })
        );
        if (!chunk || !chunk.data) {
          throw new Error("试听分片读取失败（第 " + (index + 1) + "/" + total + " 片）");
        }
        parts.push(chunk.data);
      }
      audioData = parts.join("");
    }
    if (!audioData) {
      const shape = result === null || result === undefined ? String(result) : typeof result;
      throw new Error("后端没有返回音频数据（结果类型：" + shape + "）");
    }
    $("preview-audio").src = "data:" + (result.mime || "audio/mpeg") + ";base64," + audioData;
    setStatus(
      "preview-status",
      "完成：提供商 " + (result.provider || "?") + (result.emotionApplied ? "，已应用情绪" : ""),
      "ok"
    );
    const player = $("preview-audio");
    if (player && typeof player.play === "function") player.play().catch(function () {});
  } catch (error) {
    setStatus("preview-status", "语音合成试听失败：" + errorText(error), "err");
  } finally {
    if (button) button.disabled = false;
  }
}

function on(id, handler) {
  const node = $(id);
  if (node) node.addEventListener("click", handler);
}

function bindEvents() {
  $("provider-select").addEventListener("change", function () {
    state.settings.ttsProvider = $("provider-select").value;
    renderProviderFields();
    renderPreviewOptions();
  });
  $("save-settings").addEventListener("click", saveSettings);
  $("voice-upload").addEventListener("click", uploadVoice);
  $("voice-import").addEventListener("click", importVoice);
  $("worker-test").addEventListener("click", testWorker);
  document.querySelectorAll("[data-process]").forEach(function (node) {
    node.addEventListener("click", function () {
      workerProcess(node.dataset.process, node.dataset.action);
    });
  });
  on("preview-btn", preview);
  on("tts-test", testTts);
  on("worker-refresh-models", refreshWorkerModels);
}

function applyContext(context) {
  if (!context) return;
  const title = context.pageTitle || (bridge.t && bridge.t("views.studio.title", "语音工坊"));
  if (title) {
    $("page-title").textContent = title;
    document.title = title;
  }
  if (context.displayName) {
    $("page-subtitle").textContent =
      context.displayName + " · TTS 提供商、参考音色库、翻唱 worker 与试听";
  }
}

async function main() {
  if (!bridge) {
    $("page-subtitle").textContent =
      "AstrBotPluginView bridge 未加载：请从 AstrBot 插件详情页打开本页面。";
    return;
  }
  try {
    const context = await bridge.ready();
    applyContext(context);
    await refreshSettings();
    await refreshVoices();
    bindEvents();
  } catch (error) {
    $("page-subtitle").textContent = "加载失败：" + errorText(error);
  }
}

main();
