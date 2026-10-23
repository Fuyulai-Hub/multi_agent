"""框架自测。"""

import os

# 测试会真实跑 Leader.run：全局关掉运行结束的飞书通知，避免向真实群发消息（不影响被测逻辑）
os.environ.setdefault("MAS_NOTIFY_DISABLE", "1")
