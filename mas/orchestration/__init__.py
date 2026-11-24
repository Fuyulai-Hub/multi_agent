"""LangGraph 编排层（docs/50、docs/53）。

这里只负责「调度循环」这一层：节点执行、门禁、沙箱、经验库、技能全部仍在
``mas/leader.py`` 与 ``mas/agents/`` 里，由本包按 ``flow.yaml`` 装配成 StateGraph。

2026-11-25 起这是唯一编排路径：``mas.leader`` 在 ``run()`` 里惰性导入 ``runner``，
langgraph 缺失时抛 ConfigError 提示安装，不再有 legacy 后备路径。
本 ``__init__`` 不导入任何子模块，避免只读用途（如 ``mas store``）被图依赖拖住。
"""
