# 语音工坊 · AstrBot 插件（MVP）

把 DSH「语音工坊」的语音能力接进 [AstrBot](https://docs.astrbot.app/)：在 QQ（OneBot v11 / aiocqhttp）里
用中文命令朗读文本、翻唱歌曲，并直接发出**语音条**。

- **TTS**：调用语音工坊 `POST {voiceStudioUrl}/tts-studio/api/tts`
- **翻唱**：调用 `rvc-worker` 完成上传、人声分离、RVC 换声，插件本地做 RMS 对齐 + 峰值归一化混音
- 全部能力走 HTTP，**不安装 torch / RVC / soundfile**；插件本身只依赖 `httpx`（全部请求）和 `numpy`（仅混音）

> 版本：0.1.0。本机没有安装 AstrBot，因此**未做真实 AstrBot 端到端运行**；已验证的部分（语法、导入、
> 命令注册、HTTP 请求构造、混音、翻唱全链路）见文末「验证状态」。API 导入路径均对照官方文档与
> AstrBot master 源码确认，URL 见下表。

---

## 一、已核实的 AstrBot API 事实（不要凭记忆改）

| 项目 | 结论（本插件实际使用） | 来源 |
|---|---|---|
| 插件入口 | 插件目录下固定 `main.py`；插件类继承 `Star` | [最小实例](https://docs.astrbot.app/dev/star/guides/simple.html) |
| 基类 / Context | `from astrbot.api.star import Context, Star` | 同上 |
| 事件与过滤器 | `from astrbot.api.event import filter, AstrMessageEvent`；`@filter.command("朗读", alias={"语音说"})` | [处理消息事件](https://docs.astrbot.app/dev/star/guides/listen-message-event.html) |
| 命令参数 | 命令名不能带空格；参数按空格拆分。要拿「整段剩余文本」需自己解析 `event.message_str`（本插件这样做） | 同上；[`command.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/filter/command.py) 的 `GreedyStr` |
| 语音消息段 | `from astrbot.api.message_components import Record`；`Record.fromFileSystem(path)`；发送 `yield event.chain_result([record])` | [发送消息](https://docs.astrbot.app/dev/star/guides/send-message.html)、[`components.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/message/components.py) |
| aiocqhttp 语音实现 | 适配器把 `Record` 统一 `convert_to_base64()` 成 **wav** 再作为 `base64://` 交给 OneBot | [`aiocqhttp_message_event.py`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py) |
| 插件配置 | 目录下 `_conf_schema.json`；AstrBot 会生成 `data/config/<plugin_name>_config.json`，并在实例化时传给 `__init__(self, context, config: AstrBotConfig)`；`from astrbot.api import AstrBotConfig` | [插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html) |
| schema 字段名 | `type`（必填，`string/text/int/float/bool/object/list/dict/template_list`）、`description`、`hint`、`default`、`items`、`secret`、`options`、`slider`、`invisible` 等 | 同上 |
| 插件元数据 | `metadata.yaml`：`name`（英文唯一标识）、`display_name`、`desc`、`version`、`author`、`repo`、`astrbot_version`、`support_platforms`、`short_desc` | [插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)、[发布插件](https://docs.astrbot.app/dev/star/plugin-publish.html) |
| 插件依赖 | 插件目录下 `requirements.txt`，由 AstrBot 用 pip 安装 | [插件依赖管理](https://docs.astrbot.app/dev/star/plugin-new.html) |
| 开发原则 | 不要用 `requests`，用 `aiohttp` / `httpx` 等异步库 | 同上 |
| 可视化页面 | `views/` + `context.register_web_api()` + `astrbot.api.web`（本 MVP 未使用，仅记录） | [插件可视化视图](https://docs.astrbot.app/dev/star/guides/plugin-pages.html) |
| 事件钩子 | `@filter.on_decorating_result()` 可用于「把任意回复转语音」，本 MVP 未启用 | 处理消息事件页 |

> 文档地址规律：`https://docs.astrbot.app/dev/star/guides/<页面>.html`；
> 文档源码在 `https://github.com/AstrBotDevs/AstrBot/tree/master/docs/zh/dev/star/`。

---

## 二、目录结构

```text
astrbot-plugin-voice-studio/
├── metadata.yaml        # 插件元数据（AstrBot 识别插件靠它）
├── main.py              # 全部实现：4 个命令 + HTTP 客户端 + WAV 混音
├── _conf_schema.json    # 插件配置（WebUI 可视化）
├── requirements.txt     # httpx + numpy
├── __init__.py          # 仅为本地测试/包导入，AstrBot 不依赖
├── README.md
└── tests/
    └── smoke.py         # 冒烟测试（mock AstrBot 与 httpx）
```

命令一览：

| 命令 | 说明 |
|---|---|
| `/朗读 <文本>` | 合成语音并发送语音条；别名 `/语音说` |
| `/翻唱 <歌曲绝对路径> [模型]` | 上传 → 分离 → 换声 → 混音 → 语音条 |
| `/音色` | 列出语音工坊参考音色库 |
| `/语音状态` | 检查语音工坊与 rvc-worker 连通性 |

---

## 三、安装

### 0. 前置条件

- AstrBot（onebot v11 / aiocqhttp 适配器接 QQ，NapCat / LLOneBot / Lagrange 等协议端均可）
- 语音工坊可用（DSH 侧的 HTTP 路由 `/tts-studio/api/*`），并且 AstrBot 能访问到它
- `rvc-worker` 可用（仅翻唱需要），见 `..\rvc-worker\API.md`
- AstrBot 环境里的 Python 能安装 `httpx`、`numpy`

> **目录名很重要**：AstrBot 的插件标识来自 `metadata.yaml` 的 `name`（本插件为
> `astrbot_plugin_voice_studio`），社区约定插件目录也用同名。当前交付目录叫
> `astrbot-plugin-voice-studio`，**安装时请改名为 `astrbot_plugin_voice_studio`** 后放进
> AstrBot 的 `data/plugins/`。

### 方式 A：本地目录安装（开发/自用）

```text
1) 把 astrbot-plugin-voice-studio 整个目录复制到  <AstrBot>/data/plugins/
2) 重命名为 astrbot_plugin_voice_studio
3) 打开 AstrBot WebUI -> 插件 -> 点击「重载插件」（或重启 AstrBot）
4) 插件卡片 -> 安装依赖（等价于 pip install -r requirements.txt）
5) 插件卡片 -> 配置，按下面「配置示例」填写
```

### 方式 B：打包 zip 从 WebUI 安装

把插件目录压缩成 zip（zip 内顶层目录为 `astrbot_plugin_voice_studio/`），在
WebUI「插件」页使用离线安装/上传安装，再安装依赖、配置即可。
若要发布到插件市场，需要 GitHub 仓库 + `repo` 字段，详见
[发布插件](https://docs.astrbot.app/dev/star/plugin-publish.html)。

### 依赖安装

```bash
# 在 AstrBot 使用的 Python 环境里执行
pip install -r requirements.txt
```

`numpy` 只被 `/翻唱` 的混音使用；`/朗读`、`/音色`、`/语音状态` 不依赖它。

---

## 四、配置示例

WebUI 插件详情页会按 `_conf_schema.json` 渲染表单。关键项：

```json
{
  "voiceStudioUrl": "http://127.0.0.1:6716",
  "voiceStudioToken": "",
  "workerUrl": "http://127.0.0.1:7870",
  "workerToken": "CHANGE_ME",
  "defaultProvider": "indextts",
  "defaultVoice": "voice_xxx",
  "defaultEmotion": "auto",
  "allowedGroups": ["123456789"],
  "maxDurationSec": 600,
  "maxTextLen": 1000,
  "jobTimeoutSec": 1800,
  "requestTimeoutSec": 600,
  "sendProgress": true,
  "workerModel": "shaonv.pth",
  "workerIndex": "",
  "workerF0Method": "rmvpe",
  "workerKeyShift": 0
}
```

说明：

- `voiceStudioUrl` 填 **DSH Web 服务的 origin**（不要带 `/tts-studio`）。DSH 每次启动端口可能变化，
  以 DSH Web GUI 地址为准（本机示例 `http://127.0.0.1:6716`）。
- `voiceStudioToken` / `workerToken` 是 Bearer Token：worker 用启动参数 `--token` 的那个；
  语音工坊本身没有内建鉴权，token 只有在你前面放了反向代理时才有意义。
- `allowedGroups` 为空表示不限制群；私聊始终允许。
- `defaultVoice` 可用 `/音色` 查到的 **id 或名称**。
- `defaultEmotion` 支持 `auto`（交给语音工坊判断）、标签（`happy`/`angry`/…）、带强度写法 `happy:0.8`。
- `workerModel` 留空时，插件调用 `GET /v1/models` 取 worker 上第一个模型。

### rvc-worker 启动（示例，来自 `API.md`）

```bat
runtime\python.exe rvc-worker\worker.py --port 7870 --token CHANGE_ME --rvc-root D:\rvc\RVC20260718Nvidia
```

---

## 五、OneBot v11 下的测试步骤

1. **先私聊**发 `/语音状态`，确认两行都是 `✅`。失败时先把配置里的 URL 用浏览器/`curl` 打开验证：
   ```bash
   curl -s http://127.0.0.1:6716/tts-studio/api/state
   curl -s -H "Authorization: Bearer CHANGE_ME" http://127.0.0.1:7870/v1/health
   ```
2. **TTS 自测**（不经过 AstrBot，先确认工坊本身能出声）：
   ```bash
   curl -s -X POST http://127.0.0.1:6716/tts-studio/api/tts \
     -H "Content-Type: application/json" \
     -d '{"text":"你好，我是语音工坊","provider":"indextts","sendToQQ":false}'
   ```
   返回里 `file` 是工坊机器上的本地路径，`url` 是 `/tts-studio/api/audio?path=...`。
3. **QQ 私聊**：`/朗读 你好，世界`。应收到一条语音条。
4. **QQ 群聊**：先在 `allowedGroups` 中加入群号再测（否则会收到白名单提示）。
5. **翻唱**：把歌曲放到 AstrBot 所在机器上（必须是插件进程可读的**绝对路径**），发送：
   ```text
   /翻唱 "C:\music\song.mp3"
   /翻唱 "C:\music\song.mp3" shaonv.pth
   ```
   过程会依次提示：开始 → 上传+分离 → 换声 → 混音 → 语音条。首次跑模型预热可能较慢。
6. 排错看 AstrBot 日志：插件的 `logger` 前缀是 `astrbot_plugin_voice_studio`；未预期异常会带堆栈。
   worker 侧可用 `GET /v1/jobs/{id}` 看 `status/stage/error`。

> 群聊里命令可能需要唤醒前缀（默认 `/`）。本插件的参数解析同时兼容 `/朗读` 与 `朗读` 两种写法，
> 取决于 AstrBot 的 `wake_prefix` 配置。

---

## 六、语音条发送注意事项（重要）

1. **链路是 `Record -> wav base64 -> OneBot`**：aiocqhttp 适配器（源码见上表）会把任何 `Record`
   `convert_to_base64()` 成 **wav**，再以 `base64://` 发送。也就是说：
   - 插件给 mp3/m4a 也能发（MediaResolver 负责转码），但**实际传输体积会膨胀**；
   - 音频越长，base64 负载越大，协议端/网络越容易超时。**建议先测 5~20 秒的短文本**，
     翻唱整首歌是重负载场景。
2. 本插件对 TTS 结果优先用语音工坊返回的**本地文件**；如果语音工坊在另一台机器（`file` 路径本机不存在），
   会自动回落到 `url` **流式下载**到临时目录。
3. AstrBot 文档标注语音段「暂时只接受 wav」：本插件在检测到非 wav 且本机有 `ffmpeg` 时会先转 wav；
   没有 ffmpeg 时直接交给 AstrBot 的 MediaResolver（它依赖 ffmpeg，缺失时可能发送失败）。
4. QQ 协议端对语音条有各自的限制（时长/格式/大小，例如 NapCat 对语音上传有大小限制），
   若语音条发不出，先看协议端日志；兜底方案是改成发送文件（`Comp.File`）而不是 `Record`。
5. 翻唱结果默认发 **wav**（24-bit PCM）。若你的协议端对 wav 支持差，可以自行改为发 mp3：
   `ffmpeg -i cover.wav -b:a 192k cover.mp3`，再用 `Record.fromFileSystem(mp3)`。
6. `maxDurationSec` 同时限制翻唱时长与「分离后实际时长」（防止绕过）；TTS 文本长度限制由 `maxTextLen` 控制。

---

## 七、翻唱管线（为什么插件里要混音）

`rvc-worker` 的 `API.md` 只提供 `separate` / `convert` 两类任务（没有现成的 `cover` 合成），
所以「混音、母带」按分工在客户端做：

```text
歌曲文件
  └─ POST /v1/files                        -> fileId
  └─ POST /v1/jobs {type:"separate"}        -> jobId（轮询 /v1/jobs/{id}）
       └─ GET /v1/jobs/{id}/output?name=vocal.wav|instrumental.wav  -> 下载
  └─ POST /v1/jobs {type:"convert"}（吃分离出的人声 fileId） -> converted.wav 下载
  └─ 本地混音（main.py::_mix_cover）
       1) 人声重采样到伴奏采样率（线性插值）
       2) 用「换声前的人声」RMS 对齐换声后人声响度（RVC 输出音量常漂移）
       3) 人声（居中单声道）+ 立体声伴奏
       4) 峰值归一化到 -1 dBFS（0.891）
       5) 可选 ffmpeg loudnorm 统一响度（I=-16 LUFS / TP=-1.5）
  └─ Record 语音条
```

混音逻辑移植自 `voice-studio/lib/wav.js` 的 `mixCover` / `resample` / `toMono` / `rms` / `peak`，
用 numpy 重写，读 8/16/24/32-bit PCM 与 32/64-bit IEEE float WAV，统一输出 24-bit PCM WAV。

错误处理：所有可预期错误（URL 未配置、文件不存在、worker 4xx/5xx、任务 failed、超时、音频为空、
白名单拦截、numpy 缺失等）都会回**一条中文提示**，不会静默失败；未预期异常会记 `logger.exception`
并回中文错误。

---

## 八、验证状态

已在本机（Python 3.12.14 / numpy 2.3.5，无 AstrBot、无 httpx）执行：

```text
python -m py_compile main.py __init__.py tests/smoke.py   -> exit 0
python tests/smoke.py                                     -> Ran 18 tests ... OK
```

`tests/smoke.py` 用伪造的 `astrbot`（Star/filter/事件/Record）与伪造的 `httpx.AsyncClient`
覆盖：

- `main.py` 能 import，插件类继承 `Star`；
- 4 个命令 + `语音说` 别名被 `filter.command` 注册；
- `httpx.AsyncClient` 构造参数（`follow_redirects`、`Timeout`）、Bearer 头、URL 归一化；
- `/朗读` 请求体：`text` / `provider` / `voiceId` / `emotion` / `sendToQQ:false`；
- TTS 远程 url 的流式下载回退；
- 群白名单拦截（不发起 HTTP）；
- `/音色`、`/语音状态` 文案；
- WAV 混音（采样率/声道/峰值）与 `/翻唱` 全链路（fake worker：上传→分离→下载→换声→混音→语音条）。

**未验证**（本机无 AstrBot / 无真实服务）：

- 真实 AstrBot 4.x 下插件加载、命令触发（中文命令名、`alias`）与 OneBot 语音条落地；
- 与真实语音工坊 / rvc-worker 的联调（参数名与路径已按源码/`API.md` 对齐，但未见真实响应）；
- `Record` 在不同协议端（NapCat/LLOneBot/Lagrange）的语音条表现与体积上限；
- ffmpeg / ffprobe 存在与缺失两种分支的真实表现（逻辑已按可选能力降级）；
- RVC 输出 WAV 的位深/采样率分布（代码覆盖 8/16/24/32-bit PCM 与 float32/64）。

---

## 九、故障排查

| 现象 | 排查 |
|---|---|
| 插件加载失败 | 看 AstrBot 日志；确认目录名 `astrbot_plugin_voice_studio`、`main.py` 在根目录 |
| `缺少 httpx 依赖` | 在 AstrBot 环境 `pip install -r requirements.txt`，或插件卡片「安装依赖」 |
| `未配置 voiceStudioUrl` | 填 DSH Web 的 origin，别带 `/tts-studio` |
| `/朗读` 报 404 | 语音工坊路由是 `/tts-studio/api/tts`；确认 DSH 端语音工坊插件启用 |
| `/朗读` 报“本地提供商需要参考音频” | 先在语音工坊设置页选/上传参考音色（本地提供商必须有） |
| 翻唱报 `rvc-worker 上没有可用的 RVC 模型` | 把 `.pth` 放到 RVC `assets/weights`，或配置 `workerModel` |
| 翻唱一直“分离中” | `GET /v1/jobs/{id}` 看 stage/error；worker GPU 是否被占用 |
| 语音条发不出 | 先缩短文本/歌曲；查协议端日志；见上文「语音条发送注意事项」 |

---

## 十、来源与许可

- API / 参数全部对齐本仓库 DSH 侧实现：`voice-studio/index.js`（路由与 `studio.generate`）、
  `voice-studio/lib/tts.js`、`voice-studio/lib/wav.js`、`rvc-worker/API.md`。
- 混音算法移植自语音工坊（`lib/wav.js`，MIT 项目的一部分），用 numpy 重写。
- AstrBot 是独立项目（[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)），
  本插件按其插件规范编写，未修改 AstrBot 本体。
- 本 MVP 不包含 AstrBot 插件市场发布所需的 GitHub 仓库（`repo` 字段为占位 URL），
  发布前请替换为自己的仓库地址。
