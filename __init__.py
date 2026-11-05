"""语音工坊 AstrBot 插件。

AstrBot 插件入口固定为同目录下的 ``main.py``（插件类继承 ``astrbot.api.star.Star``），
本文件只用于让目录成为常规 Python 包、便于本地静态检查与测试；
这里刻意不 import main，避免 AstrBot 以包形式加载时产生重复导入。
"""
