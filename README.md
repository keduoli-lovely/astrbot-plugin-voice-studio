# 语音工坊 · AstrBot 插件（独立版 v0.3.0）

AstrBot 原生的语音工坊插件：**不依赖 DSH**。TTS 由插件自己直接请求各提供商 HTTP API，
参考音色库放在插件目录 `data/voices/`，情绪用内置词典，翻唱只依赖独立的 `rvc-worker`，
最后通过 AstrBot 的 OneBot v11 适配器发 **Record 语音条**。

v0.3.0 新增 **可视化设置页「语音工坊」**（插件详情页 → 语音工坊）：

- 选哪个 TTS 提供商，就只显示该提供商的配置字段（其余隐藏）；
- 音色库列表 / 上传 / 删除 / 设为默认，支持从服务器路径导入；
- 翻唱 worker 地址、Token、模型、Index、F0、变调；「测试连接」与进程启停；
- 输入文本 + 选音色/情绪，直接试听（base64 data URI 播放）。

设置写入插件自己的 `data/settings.json`，读取顺序 **`data/settings.json` > AstrBot 插件配置**，
旧配置仍向后兼容。

---

## 一、已核实的 AstrBot API（不要凭记忆改）

| 项目 | 结论（本插件实际使用） | 来源 |
|---|---|---|
| 插件入口 | 插件目录下固定 `main.py`；插件类继承 `Star`，类名以 `Plugin` 结尾 | [最小实例](https://docs.astrbot.app/dev/star/guides/simple.html) |
| 插件加载方式 | `__import__("data.plugins.<目录名>.main")`，**目录名必须是合法 Python 标识符**；目录内多文件 + 相对导入可行 | [`star_manager.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/star_manager.py) |
| 基类 / Context | `from astrbot.api.star import Context, Star`；`def __init__(self, context: Context, config: AstrBotConfig)` | 最小实例页 |
| 事件与过滤器 | `from astrbot.api.event import filter, AstrMessageEvent`；`@filter.command("朗读", alias={"语音说"})` | [处理消息事件](https://docs.astrbot.app/dev/star/guides/listen-message-event.html) |
| 语音消息段 | `from astrbot.api.message_components import Record`；`Record.fromFileSystem(path)`；`yield event.chain_result([record])` | [发送消息](https://docs.astrbot.app/dev/star/guides/send-message.html)、[`components.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/message/components.py) |
| aiocqhttp 语音实现 | 适配器把 `Record` 统一转成 **wav base64** 交给 OneBot | [`aiocqhttp_message_event.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py) |
| 插件配置 | `_conf_schema.json`；AstrBot 生成 `data/config/<目录名>_config.json` 并传给 `__init__` | [插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html) |
| **插件可视化视图** | 只扫描 `views/<view_name>/index.html`；标题/描述在 `.astrbot-plugin/i18n/<locale>.json` 的 `views.<view_name>` | [插件可视化视图](https://docs.astrbot.app/dev/star/guides/plugin-pages.html) |
| **后端 Web API** | `context.register_web_api(route, handler, methods, desc)`；路由需带插件名前缀 | 同上 |
| **请求/响应 helper** | `from astrbot.api.web import request, json_response, error_response, file_response, PluginUploadFile`；`request.query.get`、`await request.json(default={})`、`await request.form()`、`await request.files()` | 同上、[`astrbot/api/web.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/api/web.py) |
| **上传** | bridge 的 `upload(endpoint, file)` 固定字段名 `file`；`PluginUploadFile` 有 `filename` / `content_length` / `await read(size)` / `await save(path, max_bytes=...)` | 同上 |
| 元数据 | `metadata.yaml`：`name`/`display_name`/`desc`/`version`/`author`/`repo`/`astrbot_version`/`support_platforms` | [插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)、[发布插件](https://docs.astrbot.app/dev/star/plugin-publish.html) |
| 依赖 | `requirements.txt`，AstrBot 用 pip 安装；官方要求用 `aiohttp`/`httpx`，不要用 requests | 插件开发指南 |

> **版本要求**：命令功能只需 `>=4.0`。**设置页**依赖「插件可视化视图」+ `context.register_web_api`
> + `astrbot.api.web`（在 AstrBot master 源码核对，官方文档未标注确切最低版本）；
> 若插件详情页看不到「语音工坊」入口，请升级 AstrBot（旧版仍可用全部命令，只是没有设置页）。

> **目录名**：交付目录是 `astrbot-plugin-voice-studio`（含连字符），安装前请改名为
> `astrbot_plugin_voice_studio`（与 `metadata.yaml` 的 `name` 一致），否则 AstrBot 无法导入。

---

## 二、已核实的提供商协议

| 提供商 | 请求 | 本机验证 |
|---|---|---|
| IndexTTS（本地 Gradio 5） | ① `POST {base}/gradio_api/upload`（multipart 字段名 `files`，失败再试 `/upload`，返回 `["<服务器路径>"]`）② `POST {base}/gradio_api/call/gen_single`，JSON `{"data":[...]}` → `{"event_id": "..."}` ③ `GET {base}/gradio_api/call/gen_single/{event_id}`（SSE，`event: complete` / `data: ...`）④ `GET {base}/gradio_api/file=<urlencode(服务器路径)>` 下载 | **真机通过**（Gradio 5.45.0，`127.0.0.1:7860`），见「十三、验证状态」 |
| GPT-SoVITS（本地 api_v2.py） | `GET {base}/tts?text=...&ref_audio_path=...&prompt_text=...&speed_factor=...`（404/405 回退 `POST {base}/tts` JSON） | 请求构造已 mock 验证；本机未启动 api_v2，**未测** |
| SiliconFlow | `POST {base}/audio/speech`（Bearer apiKey，`input` 带 CosyVoice 情绪前缀）→ 二进制 | 仅 mock（无 key） |
| MiniMax | `POST {base}/t2a_v2[?GroupId=]`（Bearer apiKey，`output_format:"hex"`，`data.audio` 为 hex） | 仅 mock（无 key） |
| MiMo | `POST {base}/chat/completions`（`api-key` + `Authorization` 双头，`audio` 参数，`choices[0].message.audio.data` base64） | 仅 mock（无 key） |
| OpenAI 兼容 | `POST {base}/audio/speech`（Bearer apiKey，`instructions` 带情绪）→ 二进制 | 仅 mock（无 key） |

情绪按 `voice-studio/lib/tts.js` / `emotion.js` 的映射移植：IndexTTS 8 维向量、
MiniMax 枚举、SiliconFlow/OpenAI 英文 instruction 前缀、MiMo 中文风格提示、GPT-SoVITS 无情绪。

---

## 三、目录结构

```text
astrbot-plugin-voice-studio/
├── metadata.yaml              # 插件元数据（version 0.3.0）
├── main.py                    # 插件类 + 6 个命令 + 翻唱编排 + 设置合并
├── common.py                  # 错误类型 / 配置读取 / 后缀 / httpx 延迟导入
├── emotion.py                 # 内置词典 + 8 维情绪 + 各提供商映射
├── tts_providers.py           # 6 个 TTS 提供商（httpx）
├── voice_store.py             # 本机参考音色库（复制/字节上传/重名/删除/元数据）
├── settings_store.py          # data/settings.json（校验 + 与 AstrBot 配置合并）
├── web_api.py                 # 设置页后端 API（register_web_api）
├── audio.py                   # WAV 编解码 / 混音 / ffmpeg 可选能力
├── worker.py                  # rvc-worker HTTP（唯一外部重活依赖）
├── _conf_schema.json          # 插件配置（设置页接管的字段标 invisible）
├── requirements.txt           # httpx + numpy
├── __init__.py
├── LICENSE
├── README.md
├── .gitignore                 # data/ 运行数据不进仓库
├── .astrbot-plugin/i18n/      # zh-CN.json / en-US.json（设置页标题/描述）
├── views/studio/              # 设置页：index.html + app.js + style.css（无第三方 JS）
├── data/                      # 运行时生成（gitignore）：voices/、voices.json、settings.json、tmp/
└── tests/
    ├── smoke.py               # 71 个离线测试（mock AstrBot / httpx / worker）
    └── real_indextts.py       # IndexTTS 真机测试（可独立运行）
```

---

## 四、安装

### 0. 前置条件

- AstrBot（本插件面向 OneBot v11 / aiocqhttp：NapCat / LLOneBot / Lagrange 等）
- 本地 TTS 需要先启动对应服务（IndexTTS Gradio 或 GPT-SoVITS api_v2）；在线 TTS 需要 API Key
- 翻唱需要 `rvc-worker` 在 RVC/MSST 机器上运行（见 `..\rvc-worker\API.md`）
- AstrBot 的 Python 环境可安装 `httpx`、`numpy`

### 1. 放入插件目录并改名

```text
把 astrbot-plugin-voice-studio/ 复制到  <AstrBot>/data/plugins/
并重命名为                            astrbot_plugin_voice_studio
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
# 或 WebUI 插件卡片里的「安装依赖」
```

### 3. 重载插件

WebUI → 插件 → 重载插件。然后打开插件详情页，点「语音工坊」进入设置页
（修改 `views/` 下的文件后，也需要重载插件才能在页面看到新内容）。

---

## 五、设置页「语音工坊」

入口：AstrBot WebUI → 插件 → 语音工坊 → 详情页里的「语音工坊」视图。

### 页面结构

1. **语音合成**：提供商下拉（indextts/gptsovits/siliconflow/minimax/mimo/openai），
   **选中哪个就只显示哪一组字段**；另有默认情绪 / 情绪强度 / 默认语速 / 默认音色；
   「保存设置」。
2. **参考音色库**：文件上传（`bridge.upload` → 后端复制进 `data/voices/`）、
   从服务器绝对路径导入、列表（名称/文件/大小/是否可用/默认标记）、删除、设为默认。
3. **翻唱 Worker**：地址 / Token / RVC 模型 / Index / F0 方法 / 变调；
   「测试连接」调用 worker `/v1/health`；可选的 RVC/MSST 子进程启动/停止
   （调用 worker `/v1/process/*`）。
4. **试听**：文本（≤300 字）+ 音色 + 情绪 → 后端合成，返回 base64 data URI 直接播放。

> 翻唱**只需要 rvc-worker 地址**；插件不访问任何 RVC WebUI，也不需要 RVC WebUI 地址。

### 设置保存位置与读取顺序

- 页面保存写入 `<插件目录>/data/settings.json`（不要手工改 AstrBot 配置里的 provider 字段）；
- 插件读取顺序：**`data/settings.json` > AstrBot 插件配置**；
- `_conf_schema.json` 里被页面接管的字段（`ttsProvider`、6 个提供商对象、6 个 worker 字段）
  标记为 `invisible: true`，只作为旧版本/命令行场景的回退；
- 可见配置项保留：`allowedGroups`、`maxTextLen`、`maxDurationSec`、`jobTimeoutSec`、
  `requestTimeoutSec`、`sendProgress`、`defaultVoice`、`defaultEmotion`、`emotionIntensity`、
  `ttsSpeed`、`voiceDir`。

### 后端 API（都注册在 `context.register_web_api`，前缀 `/{插件目录名}/`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `settings` | 读取（含 providers 元数据）/ 保存设置补丁，写 `data/settings.json` |
| GET | `voices` | 音色列表 + 当前默认音色 |
| POST | `voices/upload` | multipart `file` 上传音色（扩展名/30MB/重名校验） |
| POST | `voices/import` | `{path, name}` 从服务器绝对路径导入 |
| POST | `voices/delete` | `{id}` 或 `{name}` 删除音色（文件 + 元数据） |
| GET | `voices/audio?name=` | `file_response` 返回参考音频（试听/下载用） |
| POST | `test` | `{text, provider, voice, emotion}` → `{ok, mime, data(base64), format, provider, emotionApplied}` |
| GET | `worker/health` | 调用 rvc-worker `/v1/health` |
| POST | `worker/process` | `{process: rvc-webui\|msst, action: start\|stop, port?}` → worker `/v1/process/*` |

安全约束：

- 上传只取文件名 basename，扩展名白名单（wav/mp3/flac/m4a/ogg/opus/aac/wma/aiff/aif），
  单文件 ≤30MB；
- 试听/音频下载只通过音色库解析（id/名称），不接受任意路径；
- `provider` / `voice` 必须命中已知提供商或音色库；设置字段做类型/范围校验；
- 页面运行在 AstrBot 受限 iframe 中，全部请求走 bridge（不硬编码后端地址、不直接 fetch）。

---

## 六、插件配置（`_conf_schema.json`）

```jsonc
{
  "ttsProvider": "indextts",            // invisible：由设置页管理（回退值）
  "indextts": { "baseUrl": "http://127.0.0.1:7860", "lang": "ZH", "emoWeight": 0.8, "durationFactor": 1.0 },
  "gptsovits": { "baseUrl": "http://127.0.0.1:9880", "textLang": "zh", "promptText": "", "promptLang": "zh", "speed": 1.0 },
  "siliconflow": { "baseUrl": "https://api.siliconflow.cn/v1", "apiKey": "", "model": "...", "voice": "speech:auto", "format": "wav" },
  "minimax": { "baseUrl": "https://api.minimax.chat/v1", "apiKey": "", "groupId": "", "model": "speech-02-hd", "voice": "female-shaonv", "format": "mp3" },
  "mimo": { "baseUrl": "", "apiKey": "", "model": "mimo-v2.5-tts", "voice": "", "style": "", "format": "wav" },
  "openai": { "baseUrl": "https://api.openai.com/v1", "apiKey": "", "model": "gpt-4o-mini-tts", "voice": "alloy", "format": "mp3", "emotionInstructions": true },
  "workerUrl": "http://127.0.0.1:7870",  // 以下 6 项 invisible：由设置页管理
  "workerToken": "",
  "workerModel": "",
  "workerIndex": "",
  "workerF0Method": "rmvpe",
  "workerKeyShift": 0,

  "defaultVoice": "",
  "defaultEmotion": "auto",
  "emotionIntensity": 0.6,
  "ttsSpeed": 1.0,
  "voiceDir": "data/voices",
  "allowedGroups": [],
  "maxTextLen": 1000,
  "maxDurationSec": 600,
  "jobTimeoutSec": 1800,
  "requestTimeoutSec": 600,
  "sendProgress": true
}
```

各提供商子对象、worker 字段在设置页里编辑；`allowedGroups` 等仍在 AstrBot 插件配置页设置。

---

## 七、命令清单

| 命令 | 说明 |
|---|---|
| `/朗读 <文本>`（别名 `/语音说`） | 合成语音并发语音条 |
| `/朗读 -v <音色> -e <情绪> -i <0~1> -p <提供商> -s <语速> <文本>` | 临时覆盖音色/情绪/强度/提供商/语速 |
| `/翻唱 <歌曲绝对路径> [模型]` | 上传 rvc-worker → 分离 → 换声 → 混音 → 语音条（路径有空格用引号） |
| `/音色` | 列出本机音色库 |
| `/音色导入 <本机绝对路径> [名称]` | 复制音频进音色库（≤30MB） |
| `/音色删除 <名称\|id> [--yes]` | 删除音色，不带 `--yes` 会先确认 |
| `/语音状态` | 检查插件自身配置、音色库与 rvc-worker `/v1/health` |

示例：

```text
/朗读 今天天气不错，适合出去走走
/朗读 -e happy -i 0.8 今天真开心
/朗读 -v 小鲸鱼 你好呀
/朗读 -p openai -v alloy 你好
/音色
/音色导入 "C:\voices\xiaojingyu.wav" 小鲸鱼
/音色删除 小鲸鱼 --yes
/语音状态
/翻唱 "C:\music\song.mp3"
/翻唱 "C:\music\song.mp3" shaonv.pth
```

`-v`：本地提供商时指向音色库名称/id；在线提供商时作为该提供商 `voice` 的临时覆盖。

---

## 八、情绪

1. **显式**：`/朗读 -e happy -i 0.9 ...` 或设置页/配置 `defaultEmotion`；
2. **自动**：`defaultEmotion: auto`（默认）用内置中英文词典打分
   （开心/难过/生气/害怕/厌恶/低落/惊喜/平静 + happy/sad/angry...）；
3. **关闭**：`defaultEmotion: off`。

8 个维度：`happy, angry, sad, fear, disgust, down, surprise, calm`（另有 `neutral`）。
中文别名（高兴/开心/愤怒/生气/悲伤/难过/恐惧/害怕/反感/厌恶/低落/沮丧/惊讶/惊喜/自然/平静/中性）都可用。

---

## 九、音色库

- 默认目录：`<插件目录>/data/voices/`（`voiceDir` 可改）；元数据：`data/voices.json`。
- 导入 = **复制** 进库，不引用原文件；删除不会动你原来的音频。
- 限制：单文件 ≤ 30MB；后缀白名单见上；重名自动加序号（`音色`、`音色 (2)`…）。
- 三种导入方式：设置页上传、设置页服务器路径导入、`/音色导入` 命令。
- 删除前需要 `--yes` 二次确认；文件缺失的音色在列表中标注「文件缺失」。
- `data/` 已写入 `.gitignore`，插件更新/重装不会覆盖用户音色库（仍建议备份 `data/voices*`）。

---

## 十、翻唱管线（只依赖 rvc-worker）

```text
歌曲文件
  └─ POST /v1/files                                    -> fileId
  └─ POST /v1/jobs {type:"separate"}                    -> jobId（轮询 GET /v1/jobs/{id}）
       └─ GET /v1/jobs/{id}/output?name=vocal.wav|instrumental.wav   -> 下载
  └─ POST /v1/jobs {type:"convert"}（吃分离出的人声 fileId）        -> converted.wav
  └─ 本地混音（audio.mix_cover，numpy）
       1) 人声重采样到伴奏采样率（线性插值）
       2) 用「换声前的人声」RMS 对齐换声后音量
       3) 人声（居中单声道）+ 立体声伴奏
       4) 峰值归一化到 -1 dBFS（0.891）
       5) 可选 ffmpeg loudnorm（I=-16/TP=-1.5/LRA=11）
  └─ Record 语音条
```

`rvc-worker` 的接口/错误格式以 `..\rvc-worker\API.md` 为准；插件只调
`/v1/health`、`/v1/models`、`/v1/files`、`/v1/jobs*`、`/v1/process/*`，
**不访问 RVC WebUI，也不需要 RVC WebUI 地址**。

---

## 十一、OneBot v11 测试与语音条注意事项

1. 私聊先发 `/语音状态`：应看到 TTS 提供商、音色库、rvc-worker 三行状态。
2. 设置页「试听」可先验证 TTS 配置；也可跑真机脚本 `python tests/real_indextts.py`。
3. 私聊发 `/朗读 你好`；群聊先在 `allowedGroups` 里加群号。
4. 翻唱：歌曲必须放在 **AstrBot 插件进程可读** 的绝对路径下。
5. **语音条链路是 `Record -> wav base64 -> OneBot`**：
   - 音频越长 base64 体积越大，协议端/网络越容易超时；先测 5~20 秒；
   - 本插件 TTS 结果优先发 wav（mp3 等在有 ffmpeg 时自动转 wav）；
   - QQ 协议端各自有语音大小/时长限制，发不出时先看协议端日志，兜底可改发文件。
6. 命令可能需要唤醒前缀（默认 `/`；本插件参数解析同时兼容 `/朗读` 与 `朗读`）。

---

## 十二、设置页表单说明（无截图时的结构描述）

```text
┌ 语音工坊（页面标题，来自 i18n views.studio.title）
│  语音合成
│    [提供商 ▼ indextts]              ← 选中后只渲染该提供商的字段
│    [baseUrl] [lang] [emoWeight] [durationFactor]   ← 随提供商变化
│    [默认情绪] [情绪强度] [默认语速] [默认音色] [保存设置]
│  参考音色库
│    [选择文件] [上传音色]  | [服务器路径] [名称] [从路径导入]
│    1. 小鲸鱼  id: v1a2b3c4d · 230.7KB   [设为默认] [删除]
│  翻唱 Worker
│    [Worker 地址] [Token] [RVC 模型] [Index] [F0 方法] [变调]
│    [测试连接] [启动/停止 RVC 进程] [启动/停止 MSST 进程]
│    { /v1/health 返回的 JSON }
│  试听
│    [文本域] [音色 ▼] [情绪 ▼] [试听]  <audio controls>
└
```

---

## 十三、验证状态

### 已通过（本机真实执行）

```text
python -m py_compile *.py tests/*.py             -> exit 0
node --check views/studio/app.js                 -> exit 0
python -B tests/smoke.py                         -> Ran 71 tests in 0.437s / OK（exit 0）
python -B tests/real_indextts.py                 -> PASS（IndexTTS 真机，见下）
```

`tests/smoke.py` 覆盖（71 个用例）：

- 伪 AstrBot 环境下插件包加载（相对导入）、6 个命令注册、`语音说` 别名、独立性守卫；
- `data/settings.json`：补丁合并、优先级、非法值拒绝、损坏文件报错；
- 设置页 Web API：路由注册、settings 读写、音色上传（重名/后缀/超限/路径穿越）、
  列表/导入/删除/音频、试听 base64、worker 健康与进程启停；
- 设置页静态资源：`index.html`↔`app.js`/`style.css`、bridge 端点、i18n、schema invisible；
- 6 个提供商请求 URL/方法/请求体/鉴权头；`/朗读`、`/语音状态`、`/翻唱`（fake worker）全链路；
- 混音质量（采样率/声道/峰值 0.891）。

### 真机测试输出（IndexTTS，Gradio 5.45.0，`http://127.0.0.1:7860`）

```text
[real] IndexTTS base = http://127.0.0.1:7860
[real] ref audio     = C:\Users\keduoli\.dsh-voice-studio\refs\v_d46bc95c07914908.mp3 (230.7KB)
[real] text          = 你好，我是语音工坊，今天天气真好。
[real] emotion       = neutral
[real] provider      = indextts / format=wav / emotionApplied=False
[real] size          = 210988 bytes (206.0KB)
[real] duration      = 4.78s
[real] riff-wav ok   = True
[real] elapsed       = 11.14s
[real] PASS
```

此前同一脚本用 `--emotion happy`（走 8 维情感向量）也通过：
`emotionApplied=True`、156716 bytes、3.55s、5.47s。

真机脚本需要 httpx；本机把它装在临时目录（未改全局环境）：

```bash
python -m pip install --target %TEMP%\vs_httpx httpx
python -B tests/real_indextts.py
```

### 未验证

- **真实 AstrBot 4.x 下的设置页**：`views/` 加载、iframe bridge、`context.register_web_api`
  实际注册与 Dashboard 转发未在真实 AstrBot 上跑过（本机无 AstrBot）；
- AstrBot 支持「插件可视化视图」的确切最低版本，官方文档未标注（README 已说明升级建议）；
- **GPT-SoVITS**：本机未启动 api_v2，只有 mock 验证；
- **SiliconFlow / MiniMax / MiMo / OpenAI**：无 API Key，只有 mock 请求体验证；
- **rvc-worker 真机翻唱**：IndexTTS 正占用显存，按指示未做真机 convert/separate；
- **MiMo 默认 baseUrl**：仓库无参考默认值，配置项留空并提示按官方文档填写；
- **QQ 协议端语音条大小/时长上限**：因协议端而异，见第十一节。

---

## 十四、故障排查

| 现象 | 排查 |
|---|---|
| 插件加载失败 | 确认目录名 `astrbot_plugin_voice_studio`、`main.py` 在根目录、依赖已装 |
| 插件详情页没有「语音工坊」入口 | AstrBot 版本较旧（不支持插件可视化视图），升级 AstrBot；命令仍可用 |
| 设置页白屏 / bridge 未加载 | 必须从 AstrBot 插件详情页打开（不要直接访问文件路径） |
| 保存设置失败 | 看 AstrBot 日志（插件名过滤）；`data/settings.json` 损坏可删除后回退配置 |
| `缺少 httpx 依赖` | `pip install -r requirements.txt` |
| `/朗读` 报“音色库是空的” | 设置页上传音色，或 `/音色导入`，或换在线提供商 |
| IndexTTS 报 Gradio 上传失败 | 确认 `baseUrl` 是 Gradio 服务；`curl {base}/config` 里应含 `gen_single` |
| `/朗读` 报 401/403 | 在线提供商 `apiKey` 填错或 `baseUrl` 不对 |
| `/朗读` 报“响应里没有 data.audio” | MiniMax 国内外域名不同，检查 `baseUrl` 与 `groupId` |
| 翻唱报没有 RVC 模型 | 把 `.pth` 放进 RVC `assets/weights`，或设置页填模型 |
| 翻唱一直“分离中” | `GET /v1/jobs/{id}` 看 `status/stage/error`；GPU 是否被占用/显存不足 |
| 语音条发不出 | 缩短文本/歌曲；看 AstrBot 与协议端日志；见第十一节 |

---

## 十五、独立运行说明（不依赖 DSH）

- TTS：`tts_providers.py` 直接实现 6 个提供商，只依赖 `httpx` 与标准库；
- 音色库：插件目录 `data/voices/` + `data/voices.json`，设置页/命令自持自管；
- 设置：插件目录 `data/settings.json`，不依赖 AstrBot 配置里的 provider 字段；
- 情绪：`emotion.py` 内置词典，不查询任何 DSH 情绪服务；
- 翻唱：只依赖独立的 `rvc-worker` 服务（HTTP），不访问 RVC WebUI；
- 语音发送：AstrBot 自己的 OneBot v11 适配器 + `Record`；
- `tests/smoke.py` 有守卫用例，源码一旦重新引入 DSH 路由/配置会直接失败。

---

## 十六、来源与许可

- TTS 协议、情绪映射、混音算法移植自 DSH 语音工坊（`voice-studio/lib/tts.js`、
  `emotion.js`、`wav.js`），按 `LICENSE` 保留许可声明；
- `rvc-worker` 契约见 `..\rvc-worker\API.md`；
- AstrBot 为独立项目（[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)），
  本插件按其插件规范编写，未修改 AstrBot 本体。
