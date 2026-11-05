# 语音工坊 · AstrBot 插件（独立版 v0.2.0）

AstrBot 原生的语音工坊插件：**不依赖 DSH**，TTS 由插件自己直接请求各提供商 HTTP API，
参考音色库放在插件目录 `data/voices/`，情绪用内置词典，翻唱只依赖独立的 `rvc-worker`，
最后通过 AstrBot 的 OneBot v11 适配器发 **Record 语音条**。

- 多提供商 TTS：本地 `IndexTTS`（Gradio 5）/ `GPT-SoVITS`（api_v2），
  在线 `SiliconFlow` / `MiniMax` / `MiMo` / `OpenAI 兼容 /v1/audio/speech`
- 参考音色库：`/音色`、`/音色导入`、`/音色删除`
- 情绪：内置中英文关键词词典 + `/朗读 -e happy -i 0.8` 显式指定
- 翻唱：rvc-worker 分离 + 换声，插件本地 numpy 混音
- **不安装 torch / RVC / soundfile**；插件依赖只有 `httpx`（全部 HTTP）和 `numpy`（仅混音）

---

## 一、已核实的 AstrBot API（不要凭记忆改）

| 项目 | 结论（本插件实际使用） | 来源 |
|---|---|---|
| 插件入口 | 插件目录下固定 `main.py`；插件类继承 `Star`；类名以 `Plugin` 结尾即可被识别 | [最小实例](https://docs.astrbot.app/dev/star/guides/simple.html) |
| 插件加载方式 | `__import__("data.plugins.<目录名>.main")`，因此 **目录名必须是合法 Python 标识符**；目录内多文件 + 相对导入可行 | [`star_manager.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/star_manager.py) |
| 基类 / Context | `from astrbot.api.star import Context, Star`；`def __init__(self, context: Context, config: AstrBotConfig)` | 最小实例页 |
| 事件与过滤器 | `from astrbot.api.event import filter, AstrMessageEvent`；`@filter.command("朗读", alias={"语音说"})` | [处理消息事件](https://docs.astrbot.app/dev/star/guides/listen-message-event.html) |
| 命令参数 | 命令名不能带空格；参数按空格拆分。要拿「整段剩余文本」需自己解析 `event.message_str`（本插件实现 `parse_read_args`） | 同上 |
| 语音消息段 | `from astrbot.api.message_components import Record`；`Record.fromFileSystem(path)`；`yield event.chain_result([record])` | [发送消息](https://docs.astrbot.app/dev/star/guides/send-message.html)、[`components.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/message/components.py) |
| aiocqhttp 语音实现 | 适配器把 `Record` 统一 `convert_to_base64()` 成 **wav**，以 `base64://` 交给 OneBot | [`aiocqhttp_message_event.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py) |
| 插件配置 | `_conf_schema.json`；AstrBot 生成 `data/config/<目录名>_config.json` 并传给 `__init__`；`from astrbot.api import AstrBotConfig, logger` | [插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html) |
| schema 字段 | `type`（必填：`string/int/float/bool/object/list` 等）、`description`、`hint`、`default`、`items`、`secret`、`options`、`labels`、`slider` | 同上 |
| 元数据 | `metadata.yaml`：`name`（英文唯一）、`display_name`、`desc`、`version`、`author`、`repo`、`astrbot_version`、`support_platforms` | [插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)、[发布插件](https://docs.astrbot.app/dev/star/plugin-publish.html) |
| 依赖 | 插件目录 `requirements.txt`，AstrBot 用 pip 安装；官方要求用 `aiohttp`/`httpx`，**不要用 requests** | 插件开发指南 |

> 交付目录名是 `astrbot-plugin-voice-studio`（含连字符），**安装到 AstrBot 前请改名为
> `astrbot_plugin_voice_studio`**（与 `metadata.yaml` 的 `name` 一致），否则 AstrBot 无法
> 以模块方式导入。

---

## 二、已核实的提供商协议

| 提供商 | 请求 | 本机验证 |
|---|---|---|
| IndexTTS（本地 Gradio 5） | ① `POST {base}/gradio_api/upload`（multipart 字段名 `files`，失败再试 `/upload`，返回 `["<服务器路径>"]`）② `POST {base}/gradio_api/call/gen_single`，JSON `{"data":[...]}` → `{"event_id": "..."}` ③ `GET {base}/gradio_api/call/gen_single/{event_id}`（SSE，`event: complete` / `data: ...`）④ `GET {base}/gradio_api/file=<urlencode(服务器路径)>` 下载 | **真机通过**（Gradio 5.45.0，`127.0.0.1:7860`），见「九、真机测试」 |
| GPT-SoVITS（本地 api_v2.py） | `GET {base}/tts?text=...&ref_audio_path=...&prompt_text=...&speed_factor=...`（404/405 时回退 `POST {base}/tts` JSON） | 请求构造已 mock 验证；本机未启动 api_v2，**未测** |
| SiliconFlow | `POST {base}/audio/speech`（Bearer apiKey，`input` 带 CosyVoice 情绪前缀）→ 二进制 | 仅 mock（无 key） |
| MiniMax | `POST {base}/t2a_v2[?GroupId=]`（Bearer apiKey，`output_format:"hex"`，`data.audio` 为 hex） | 仅 mock（无 key） |
| MiMo | `POST {base}/chat/completions`（`api-key` + `Authorization` 双头，`audio` 参数，返回 `choices[0].message.audio.data` base64） | 仅 mock（无 key） |
| OpenAI 兼容 | `POST {base}/audio/speech`（Bearer apiKey，`instructions` 带情绪）→ 二进制 | 仅 mock（无 key） |

情绪参数按 `voice-studio/lib/tts.js` / `emotion.js` 的映射移植：

| 情绪形态 | 用法 |
|---|---|
| IndexTTS | 8 维情感向量（happy/angry/sad/fear/disgust/down/surprise/calm），命中维度=`强度` |
| MiniMax | `voice_setting.emotion` 枚举（fear→fearful 等） |
| SiliconFlow / OpenAI | `Can you say it with a ... emotion? <|endofprompt|>` 前缀 / `instructions` |
| MiMo | 中文风格提示（`明显/非常强烈地 + 用开心、轻快的语气`） |
| GPT-SoVITS | 不支持情绪参数（`emotionApplied=False`） |

---

## 三、目录结构

```text
astrbot-plugin-voice-studio/
├── metadata.yaml          # 插件元数据（name/display_name/version/repo...）
├── main.py                # 插件类 + 6 个命令 + 翻唱编排
├── common.py              # 错误类型 / 配置读取 / 后缀 / httpx 延迟导入
├── emotion.py             # 内置词典 + 8 维情绪 + 各提供商映射
├── tts_providers.py       # 6 个 TTS 提供商（httpx）
├── voice_store.py         # 本机参考音色库（复制/重名/删除/元数据）
├── audio.py               # WAV 编解码 / 混音 / ffmpeg 可选能力
├── worker.py              # rvc-worker HTTP（唯一外部重活依赖）
├── _conf_schema.json      # 插件配置（WebUI 可视化）
├── requirements.txt       # httpx + numpy
├── __init__.py
├── LICENSE
├── README.md
├── .gitignore             # data/ 运行数据不进仓库
├── data/                  # 运行时生成（音色库 voices/、tmp/），已 gitignore
└── tests/
    ├── smoke.py           # 48 个离线冒烟测试（mock AstrBot/httpx）
    └── real_indextts.py   # IndexTTS 真机测试（可独立运行）
```

---

## 四、安装

### 0. 前置条件

- AstrBot（本插件面向 OneBot v11 / aiocqhttp：NapCat / LLOneBot / Lagrange 等协议端）
- 本地 TTS 需要先启动对应服务（IndexTTS Gradio 或 GPT-SoVITS api_v2）；在线 TTS 需要 API Key
- 翻唱需要 `rvc-worker` 在 RVC/MSST 机器上运行（见 `..\rvc-worker\API.md`）
- AstrBot 的 Python 环境可安装 `httpx`、`numpy`

### 1. 放入插件目录并改名

```text
把 astrbot-plugin-voice-studio/ 整个复制到  <AstrBot>/data/plugins/
并重命名为                            astrbot_plugin_voice_studio
```

重命名原因是 AstrBot 用 `data.plugins.<目录名>.main` 导入（见第一节表格）；
目录名带连字符会导入失败。

### 2. 安装依赖

```bash
# 用 AstrBot 自己的 Python 环境
pip install -r requirements.txt
# 或 WebUI 插件卡片里的「安装依赖」
```

### 3. 重载插件并配置

WebUI → 插件 → 重载插件；打开插件配置，按下一节填写。

---

## 五、配置示例

```jsonc
{
  "ttsProvider": "indextts",

  "indextts": {
    "baseUrl": "http://127.0.0.1:7860",
    "lang": "ZH",
    "emoWeight": 0.8,
    "durationFactor": 1.0
  },
  "gptsovits": {
    "baseUrl": "http://127.0.0.1:9880",
    "textLang": "zh",
    "promptText": "",
    "promptLang": "zh",
    "speed": 1.0
  },
  "siliconflow": {
    "baseUrl": "https://api.siliconflow.cn/v1",
    "apiKey": "",
    "model": "FunAudioLLM/CosyVoice2-0.5B",
    "voice": "speech:auto",
    "format": "wav"
  },
  "minimax": {
    "baseUrl": "https://api.minimax.chat/v1",
    "apiKey": "",
    "groupId": "",
    "model": "speech-02-hd",
    "voice": "female-shaonv",
    "format": "mp3"
  },
  "mimo": {
    "baseUrl": "",
    "apiKey": "",
    "model": "mimo-v2.5-tts",
    "voice": "",
    "format": "wav"
  },
  "openai": {
    "baseUrl": "https://api.openai.com/v1",
    "apiKey": "",
    "model": "gpt-4o-mini-tts",
    "voice": "alloy",
    "format": "mp3",
    "emotionInstructions": true
  },

  "defaultVoice": "",        // 本地提供商：音色库 id/名称；留空用第一个可用音色
  "defaultEmotion": "auto",  // auto | off | happy | ... | 中文别名
  "emotionIntensity": 0.6,
  "ttsSpeed": 1.0,
  "voiceDir": "data/voices",

  "workerUrl": "http://127.0.0.1:7870",
  "workerToken": "",
  "workerModel": "",
  "workerIndex": "",
  "workerF0Method": "rmvpe",
  "workerKeyShift": 0,

  "allowedGroups": [],
  "maxTextLen": 1000,
  "maxDurationSec": 600,
  "jobTimeoutSec": 1800,
  "requestTimeoutSec": 600,
  "sendProgress": true
}
```

| 配置 | 说明 |
|---|---|
| `ttsProvider` | `indextts` / `gptsovits` / `siliconflow` / `minimax` / `mimo` / `openai` |
| 各提供商子对象 | `baseUrl` 必填；在线提供商还要 `apiKey`、`model`、`voice`；`format` 是输出格式 |
| `defaultVoice` | **仅本地提供商**使用（音色库 id/名称）；在线提供商的 `voice` 填在各自子对象里 |
| `defaultEmotion` | `auto`=内置词典自动判断；`off`=不加情绪；也可写 `happy`/`开心` 等固定情绪 |
| `emotionIntensity` / `ttsSpeed` | 默认情绪强度 / 默认语速，可被命令参数覆盖 |
| `voiceDir` | 音色库目录（相对插件目录），元数据自动写到同级 `data/voices.json` |
| `allowedGroups` | 群聊白名单，空=不限制；私聊不限制 |
| `maxTextLen` | 单次朗读最大字数（超出截断并提示） |
| `maxDurationSec` / `jobTimeoutSec` | 翻唱时长上限 / worker 任务等待上限 |
| `workerToken` | rvc-worker `--token` 对应的 Bearer Token |
| `workerModel` | RVC `.pth` id/文件名；留空取 `GET /v1/models` 第一个模型 |

---

## 六、命令清单

| 命令 | 说明 |
|---|---|
| `/朗读 <文本>`（别名 `/语音说`） | 合成语音并发语音条 |
| `/朗读 -v <音色> -e <情绪> -i <0~1> -p <提供商> -s <语速> <文本>` | 临时覆盖音色/情绪/强度/提供商/语速 |
| `/翻唱 <歌曲绝对路径> [模型]` | 上传 rvc-worker → 分离 → 换声 → 混音 → 语音条（路径有空格用引号） |
| `/音色` | 列出本机音色库 |
| `/音色导入 <本机绝对路径> [名称]` | 复制音频进音色库（≤30MB，wav/mp3/flac/m4a/ogg…） |
| `/音色删除 <名称\|id> [--yes]` | 删除音色，不带 `--yes` 会先确认 |
| `/语音状态` | 检查本插件配置、音色库与 rvc-worker `/v1/health` |

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

`/朗读` 的 `-v`：
- 当前提供商是本地（indextts/gptsovits）时，指向音色库里的 **名称/id**；
- 当前提供商是在线时，作为该提供商 `voice` 字段的临时覆盖（如 `-v alloy`）。

---

## 七、音色库

- 默认目录：`<插件目录>/data/voices/`（`voiceDir` 可改）；元数据：`data/voices.json`。
- 导入 = **复制** 进库，不引用原文件；删除不会影响你原来的音频。
- 限制：单个文件 ≤ 30MB；后缀必须属于 `wav/mp3/flac/m4a/ogg/opus/aac/wma/aiff/aif`。
- 重名自动加序号：`音色`、`音色 (2)`、`音色 (3)`…；`resolve` 支持 id、名称、文件名（不含后缀）、大小写不敏感。
- 删除前需要 `--yes` 二次确认；文件缺失的音色在列表中标注 `[文件缺失]`。

> `data/` 已写入 `.gitignore`，插件更新/重装时不会覆盖用户的音色库
> （但仍建议自行备份 `data/voices*`）。

---

## 八、情绪

1. **显式**：`/朗读 -e happy -i 0.9 ...` 或配置 `defaultEmotion: happy`；
2. **自动**：`defaultEmotion: auto`（默认）时用内置中英文词典打分
   （开心/难过/生气/害怕/厌恶/低落/惊喜/平静 + happy/sad/angry...）；
3. **关闭**：`defaultEmotion: off`。

8 个维度：`happy, angry, sad, fear, disgust, down, surprise, calm`（另有 `neutral`）。
中文别名（高兴/开心/愤怒/生气/悲伤/难过/恐惧/害怕/反感/厌恶/低落/沮丧/惊讶/惊喜/自然/平静/中性）都可用。
映射到各提供商的细节见第二节表格。

---

## 九、翻唱管线（只依赖 rvc-worker）

```text
歌曲文件
  └─ POST /v1/files                                    -> fileId
  └─ POST /v1/jobs {type:"separate"}                    -> jobId（轮询 GET /v1/jobs/{id}）
       └─ GET /v1/jobs/{id}/output?name=vocal.wav|instrumental.wav   -> 下载
  └─ POST /v1/jobs {type:"convert"}（吃分离出的人声 fileId）        -> converted.wav
  └─ 本地混音（audio.mix_cover，numpy）
       1) 人声重采样到伴奏采样率（线性插值）
       2) 用「换声前的人声」RMS 对齐换声后音量（RVC 输出音量会漂）
       3) 人声（居中单声道）+ 立体声伴奏
       4) 峰值归一化到 -1 dBFS（0.891）
       5) 可选 ffmpeg loudnorm（I=-16/TP=-1.5/LRA=11）
  └─ Record 语音条
```

`rvc-worker` 的接口/错误格式以 `..\rvc-worker\API.md` 为准；插件只调
`/v1/health`、`/v1/models`、`/v1/files`、`/v1/jobs*`，**不调 DSH 任何接口**。

---

## 十、OneBot v11 测试与语音条注意事项

1. 私聊先发 `/语音状态`：应看到 TTS 提供商、音色库、rvc-worker 三行状态。
2. TTS 自测（不经过 AstrBot，确认真机服务本身可用）：
   ```bash
   curl -s -X POST http://127.0.0.1:7860/gradio_api/call/gen_single \
     -H "Content-Type: application/json" -d '{"data":[...]}'
   ```
   或直接跑本仓库的真机脚本：`python tests/real_indextts.py`。
3. 私聊发 `/朗读 你好`；群聊先在 `allowedGroups` 里加群号。
4. 翻唱：歌曲必须放在 **AstrBot 插件进程可读** 的绝对路径下。
5. **语音条链路是 `Record -> wav base64 -> OneBot`**（aiocqhttp 适配器源码见第一节）：
   - 音频越长 base64 体积越大，协议端/网络越容易超时；先测 5~20 秒；
   - 本插件 TTS 结果优先发 wav（mp3 等在有 ffmpeg 时自动转 wav）；
   - QQ 协议端各自有语音大小/时长限制，发不出时先看协议端日志，兜底可改发文件。
6. 命令可能需要唤醒前缀（默认 `/`；本插件参数解析同时兼容 `/朗读` 与 `朗读`）。

---

## 十一、验证状态

### 已通过（本机真实执行）

```text
python -m py_compile *.py tests/*.py      -> exit 0
python -B tests/smoke.py                  -> Ran 48 tests ... OK（离线 mock，0.35s）
python -B tests/real_indextts.py          -> PASS（IndexTTS 真机）
```

`tests/smoke.py` 覆盖（48 个用例）：

- 伪 AstrBot（Star/filter/事件/Record）下插件包加载（相对导入）、6 个命令注册、`语音说` 别名；
- **独立性守卫**：扫描源码，确认没有 `tts-studio` / `voiceStudioUrl` / `voiceStudioToken` / `/api/tts`；
- 参数解析（`-v/-e/-i/-p/-s`、引号路径、`--yes`）；
- 情绪：词典分类、显式覆盖、8 维向量、MiniMax 枚举、CosyVoice 前缀、MiMo 风格；
- 音色库：导入复制、重名加序号、按 id/名称/文件名解析、删除、非法后缀、>30MB 拒绝；
- 6 个提供商请求 URL/方法/请求体/鉴权头（含 IndexTTS Gradio 上传→call→SSE→file 全流程）；
- `/朗读` 全链路（provider mock）、群白名单、空音色库中文错误；
- `/语音状态`（插件自身配置 + worker 健康）；
- `/翻唱` 全链路（fake worker：separate → convert → 下载 → 混音 → Record）+ 混音峰值/声道断言。

### 真机测试输出（IndexTTS，Gradio 5.45.0，`http://127.0.0.1:7860`）

```text
[real] IndexTTS base = http://127.0.0.1:7860
[real] ref audio     = C:\Users\keduoli\.dsh-voice-studio\refs\v_d46bc95c07914908.mp3 (230.7KB)
[real] text          = 你好，我是语音工坊，今天天气真好，我们出去走走吧。
[real] emotion       = neutral
[real] provider      = indextts / format=wav / emotionApplied=False
[real] size          = 323628 bytes (316.0KB)
[real] duration      = 7.34s
[real] riff-wav ok   = True
[real] elapsed       = 18.87s     # 首次含模型预热
[real] PASS

# 第二次：显式情绪 happy（走 8 维情感向量）
[real] text          = 今天真开心，我们出去玩吧！
[real] emotion       = 开心(0.70, explicit)
[real] provider      = indextts / format=wav / emotionApplied=True
[real] size          = 156716 bytes (153.0KB)
[real] duration      = 3.55s
[real] riff-wav ok   = True
[real] elapsed       = 5.47s
[real] PASS
```

真机脚本需要 httpx；本机运行时把 httpx 装到临时目录（未改全局环境）：

```bash
python -m pip install --target %TEMP%\vs_httpx httpx
python -B tests/real_indextts.py
```

### 未验证

- **GPT-SoVITS**：本机未启动 api_v2（默认 9880），只有 mock 验证；
- **SiliconFlow / MiniMax / MiMo / OpenAI**：无 API Key，只有 mock 验证请求体；
- **rvc-worker 真机翻唱**：IndexTTS 正占用显存，convert/separate 真机测试可能 OOM，
  按要求只做了 fake worker 全链路与混音测试；
- **真实 AstrBot + OneBot 下的命令触发与语音条落地**：本机无 AstrBot；
- **MiMo 默认 baseUrl**：仓库里没有可参考的默认值，配置项留空并提示按官方文档填写；
- **QQ 协议端语音条大小/时长上限**：因协议端而异，见第十节。

---

## 十二、故障排查

| 现象 | 排查 |
|---|---|
| 插件加载失败 | 确认目录名 `astrbot_plugin_voice_studio`、`main.py` 在根目录、依赖已装 |
| `缺少 httpx 依赖` | `pip install -r requirements.txt` |
| `/朗读` 报“音色库是空的” | `/音色导入 <路径>` 或改 `ttsProvider` 为在线提供商 |
| IndexTTS 报 Gradio 上传失败 | 确认 `baseUrl` 是 Gradio（不是 WebUI 的其它端口）；先 `curl {base}/config` 看是否有 `gen_single` |
| `/朗读` 报 401/403 | 在线提供商 `apiKey` 填错或 `baseUrl` 不对 |
| `/朗读` 报“响应里没有 data.audio” | MiniMax 国内/国际站域名不同，检查 `baseUrl` 与 `groupId` |
| 翻唱报没有 RVC 模型 | 把 `.pth` 放进 RVC `assets/weights`，或配置 `workerModel` |
| 翻唱一直“分离中” | `GET /v1/jobs/{id}` 看 `status/stage/error`；GPU 是否被占用/显存不足 |
| 语音条发不出 | 缩短文本/歌曲；看 AstrBot 与协议端日志；见第十节 |

---

## 十三、独立运行说明（不依赖 DSH）

- TTS：插件内直接实现 6 个提供商，`tts_providers.py` 只依赖 `httpx` 与标准库；
- 音色库：插件目录 `data/voices/` + `data/voices.json`，自持自管；
- 情绪：`emotion.py` 内置词典，不查询任何 DSH 情绪服务；
- 翻唱：只依赖独立的 `rvc-worker` 服务（HTTP），不依赖 DSH；
- 语音发送：AstrBot 自己的 OneBot v11 适配器 + `Record`；
- `tests/smoke.py` 里有守卫用例，源码一旦重新引入 DSH 路由/配置会直接失败。

---

## 十四、来源与许可

- TTS 协议、情绪映射、混音算法移植自 DSH 语音工坊（`voice-studio/lib/tts.js`、
  `emotion.js`、`wav.js`），按 `LICENSE` 保留许可声明；
- `rvc-worker` 契约见 `..\rvc-worker\API.md`；
- AstrBot 为独立项目（[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)），
  本插件按其插件规范编写，未修改 AstrBot 本体。
