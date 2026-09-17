"""评测体系（开发期工具，不打进 wheel：`pyproject.toml` 只 package `src/websearch`）。

    eval/queries.yaml   金标集（标注纪律与口径取舍写在文件头）
    eval/metrics.py     纯指标数学，无 IO，可单测手算对照
    eval/run.py         跑分 + 消融对照 + 缓存探针
    eval/compare.py     与 reports/baseline.json 对比，回归超阈值 exit 1
    eval/reports/       报告快照（baseline.json 入库，latest.json 不入库）

这个 `__init__.py` 存在的唯一理由是消歧：没有它，`eval/` 是命名空间包，
mypy 会把 `eval/metrics.py` 同时解析成 `metrics` 与 `eval.metrics` 两个模块名而直接报错
（"Source file found twice under different module names"）。加上它，
`python -m eval.run`、pytest 的 `pythonpath=["."]`、mypy 三方口径就一致了。
"""
