"""情绪解析 —— 从 DSH 语音工坊 ``lib/emotion.js`` 移植为纯 Python。

只用本插件自带的词典，不查询任何 DSH 情绪插件/HTTP 接口：

1. 显式指定（命令参数 ``-e`` / 配置 ``defaultEmotion``）优先级最高；
2. ``defaultEmotion: auto`` 时用内置中英文关键词词典打分；
3. ``defaultEmotion: off/manual`` 时不加情绪（neutral）。

八个维度沿用 IndexTTS 的情感向量：happy/angry/sad/fear/disgust/down/surprise/calm。
其余的提供商按 ``tts.js`` 的映射转成 MiniMax 枚举 / CosyVoice 前缀 / MiMo 风格提示。
"""

from __future__ import annotations

from typing import Any

from .common import VoiceStudioError

DIMENSIONS = ["happy", "angry", "sad", "fear", "disgust", "down", "surprise", "calm"]

ALIASES = {
    "高兴": "happy",
    "开心": "happy",
    "愤怒": "angry",
    "生气": "angry",
    "悲伤": "sad",
    "难过": "sad",
    "恐惧": "fear",
    "害怕": "fear",
    "反感": "disgust",
    "厌恶": "disgust",
    "低落": "down",
    "沮丧": "down",
    "惊讶": "surprise",
    "惊喜": "surprise",
    "自然": "calm",
    "平静": "calm",
    "中性": "neutral",
}

EMOTION_LABELS = {
    "happy": "开心",
    "angry": "生气",
    "sad": "悲伤",
    "fear": "害怕",
    "disgust": "厌恶",
    "down": "低落",
    "surprise": "惊喜",
    "calm": "平静",
    "neutral": "中性",
}

LEXICON = {
    "happy": ["开心", "高兴", "快乐", "哈哈", "太好了", "喜欢", "棒", "耶", "愉快", "幸福", "happy", "great", "nice"],
    "angry": ["生气", "愤怒", "气死", "烦死", "滚", "讨厌", "混蛋", "可恶", "angry", "mad"],
    "sad": ["难过", "伤心", "哭", "眼泪", "遗憾", "可惜", "sad", "cry"],
    "fear": ["害怕", "恐怖", "可怕", "吓", "危险", "担心", "紧张", "fear", "scared"],
    "disgust": ["恶心", "厌恶", "反感", "嫌弃", "disgusting"],
    "down": ["累", "疲惫", "没劲", "丧", "无聊", "孤独", "失落", "低落", "tired"],
    "surprise": ["哇", "竟然", "居然", "没想到", "惊喜", "天哪", "wow", "surprise"],
    "calm": ["平静", "安静", "慢慢", "温柔", "轻轻", "calm", "gently"],
}

NEGATIVE_ZH = {
    "happy": "用开心、轻快的语气",
    "angry": "用生气、强硬的语气",
    "sad": "用悲伤、难过的语气",
    "fear": "用害怕、紧张的语气",
    "disgust": "用厌恶、嫌弃的语气",
    "down": "用低沉、疲惫的语气",
    "surprise": "用惊喜、惊讶的语气",
    "calm": "用平静、温柔的语气",
}

NEGATIVE_EN = {
    "happy": "Can you say it with a happy emotion?",
    "angry": "Can you say it with an angry emotion?",
    "sad": "Can you say it with a sad emotion?",
    "fear": "Can you say it with a fearful emotion?",
    "disgust": "Can you say it with a disgusted emotion?",
    "down": "Can you say it in a low, tired voice?",
    "surprise": "Can you say it with a surprised emotion?",
    "calm": "Can you say it calmly and gently?",
}

MINIMAX_ENUM = {
    "happy": "happy",
    "angry": "angry",
    "sad": "sad",
    "fear": "fearful",
    "disgust": "disgusted",
    "surprise": "surprised",
    "calm": "calm",
    "down": "sad",
}

VALID_LABELS = set(DIMENSIONS) | {"neutral"}


def classify_text(text: str) -> dict:
    """按内置词典打分，返回 {label, intensity, source}。"""

    source = str(text or "").lower()
    scores: dict[str, int] = {}
    for label, words in LEXICON.items():
        score = 0
        for word in words:
            if word.lower() in source:
                score += 1
        if score > 0:
            scores[label] = score
    if not scores:
        return {"label": "neutral", "intensity": 0.0, "source": "default"}
    label, score = sorted(scores.items(), key=lambda item: item[1], reverse=True)[0]
    intensity = min(0.95, 0.35 + score * 0.15)
    return {"label": label, "intensity": intensity, "source": "lexicon"}


def normalize_explicit(value: Any) -> dict | None:
    """把字符串 / {label,intensity} 规范化为情绪对象；无法识别返回 None。"""

    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        label = raw.lower()
        if label in {"neutral", "none", "off"}:
            return {"label": "neutral", "intensity": 0.0, "source": "explicit"}
        if label in VALID_LABELS:
            return {
                "label": label,
                "intensity": 0.0 if label == "neutral" else 0.7,
                "source": "explicit",
            }
        if raw in ALIASES:
            mapped = ALIASES[raw]
            return {
                "label": mapped,
                "intensity": 0.0 if mapped == "neutral" else 0.7,
                "source": "explicit",
            }
        for label_key, zh in EMOTION_LABELS.items():
            if zh == raw:
                return {
                    "label": label_key,
                    "intensity": 0.0 if label_key == "neutral" else 0.7,
                    "source": "explicit",
                }
        return None
    if isinstance(value, dict):
        raw_label = str(value.get("label") or value.get("emotion") or value.get("mood") or "").strip()
        if not raw_label:
            return None
        label = (ALIASES.get(raw_label) or raw_label).lower()
        if label not in VALID_LABELS:
            return None
        try:
            intensity = float(value.get("intensity", value.get("weight", 0.7)))
        except (TypeError, ValueError):
            intensity = 0.7
        intensity = min(1.0, max(0.0, intensity))
        if label == "neutral":
            intensity = 0.0
        return {"label": label, "intensity": intensity, "source": "explicit"}
    return None


def resolve_emotion(
    text: str,
    explicit: Any = None,
    mode: str = "auto",
    default_intensity: float = 0.6,
) -> dict:
    """解析最终情绪：显式 > 词典（auto）> neutral。

    Args:
        text: 待朗读文本，用于自动判断。
        explicit: ``-e`` 参数 / 配置里写死的标签或对象。
        mode: ``auto`` | ``off`` | ``manual``；也可传一个合法标签（如 ``happy``）。
        default_intensity: 词典命中但调用方没给强度时的兜底。
    """

    direct = normalize_explicit(explicit)
    if direct is not None:
        if direct["label"] != "neutral" and direct["source"] == "explicit" and direct["intensity"] == 0.0:
            direct["intensity"] = max(0.0, min(1.0, float(default_intensity)))
        return direct

    mode_text = str(mode or "").strip().lower()
    if mode_text in {"off", "manual", "none"}:
        return {"label": "neutral", "intensity": 0.0, "source": "off"}
    if mode_text and mode_text != "auto":
        # defaultEmotion 直接写成标签 / 中文别名的情况
        config_direct = normalize_explicit(mode_text)
        if config_direct is None:
            raise VoiceStudioError(
                f"无法识别的情绪「{mode_text}」，可选：auto/off/neutral/"
                + "/".join(DIMENSIONS)
                + "，或中文（开心/生气/悲伤/害怕/厌恶/低落/惊喜/平静）。"
            )
        if config_direct["label"] != "neutral":
            config_direct["intensity"] = max(0.0, min(1.0, float(default_intensity)))
        return config_direct

    classified = classify_text(text)
    if classified["label"] != "neutral" and classified["intensity"] <= 0:
        classified["intensity"] = min(0.95, max(0.0, float(default_intensity)))
    return classified


def to_vector(emotion: dict | None) -> list[float]:
    """IndexTTS 8 维情感向量：命中的那一维 = 强度。"""

    vector = [0.0] * len(DIMENSIONS)
    if not emotion:
        return vector
    label = str(emotion.get("label") or "neutral")
    if label == "neutral" or label not in EMOTION_LABELS:
        return vector
    try:
        intensity = float(emotion.get("intensity") or 0.0)
    except (TypeError, ValueError):
        intensity = 0.0
    vector[DIMENSIONS.index(label)] = min(1.0, max(0.05, intensity or 0.6))
    return vector


def to_minimax(emotion: dict | None) -> str:
    if not emotion or str(emotion.get("label") or "") in {"", "neutral"}:
        return ""
    return MINIMAX_ENUM.get(str(emotion.get("label")), "")


def to_cosy_prefix(emotion: dict | None) -> str:
    """CosyVoice2 风格前缀：``<instruction> <|endofprompt|>``。"""

    if not emotion or str(emotion.get("label") or "") in {"", "neutral"}:
        return ""
    instruction = NEGATIVE_EN.get(str(emotion.get("label")))
    return f"{instruction} <|endofprompt|>" if instruction else ""


def to_mimo_style(emotion: dict | None) -> str:
    if not emotion or str(emotion.get("label") or "") in {"", "neutral"}:
        return ""
    base = NEGATIVE_ZH.get(str(emotion.get("label")))
    if not base:
        return ""
    try:
        intensity = float(emotion.get("intensity") or 0.6)
    except (TypeError, ValueError):
        intensity = 0.6
    if intensity > 0.8:
        degree = "非常强烈地"
    elif intensity > 0.55:
        degree = "明显"
    else:
        degree = "轻微"
    return f"{degree}{base}"


def describe_emotion(emotion: dict | None) -> str:
    if not emotion or str(emotion.get("label") or "neutral") == "neutral":
        return "neutral"
    label = str(emotion.get("label") or "neutral")
    try:
        intensity = float(emotion.get("intensity") or 0.0)
    except (TypeError, ValueError):
        intensity = 0.0
    return f"{EMOTION_LABELS.get(label, label)}({intensity:.2f}, {emotion.get('source', '?')})"
