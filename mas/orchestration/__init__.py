"""LangGraph 编排层（docs/50）。

这里只替换「调度循环」这一层：节点执行、门禁、沙箱、经验库、技能全部仍在
``mas/leader.py`` 与 ``mas/agents/`` 里，由本包按 ``flow.yaml`` 装配成 StateGraph。

本 ``__init__`` 刻意不导入任何子模块：``mas.leader`` 在模块级只依赖
``orchestration.decision``（纯标准库）；langgraph 只在 ``runtime.orchestrator``
为 ``langgraph`` 时才被导入（runner 里惰性 import），默认的 legacy 路径不需要它。
"""
