"""workers —— Worker 文件包注册中心（文件为唯一权威，完全去 DB）。

Worker 的定义不再落库：每个 Worker 是 data/workers/<name>/ 下的一个文件包，
含版本目录（v1、v2…）、WORKER.md、sub_workers/、references/、tests/。
本包提供：
- registry：目录扫描 + mtime 指纹缓存 + 版本构建/启停/文件读写（带路径安全）
- router：/workers 管理端点（列表/详情/文件树/文件编辑/版本管理）
"""
