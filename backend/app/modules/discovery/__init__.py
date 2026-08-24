"""discovery —— 能力发现与装配（模块详细设计 §1.2）。

indexer 在 capabilities/service.py（注册时写向量）；本包提供：
- retriever：任务文本 → 语义 Top-K 能力（含 search_more_tools 元工具化入口）
- assembler：工具描述区装配（pinned + Top-K，tool_budget 封顶）+ L1/L2 压缩
"""
