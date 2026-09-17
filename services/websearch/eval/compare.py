"""回归门禁：拿新报告与基线对比，`nDCG@10` 或 `recall@50` 掉超阈值就 exit 1。

    uv run python -m eval.run --json eval/reports/latest.json
    uv run python -m eval.compare --report eval/reports/latest.json
    uv run python -m eval.compare --report latest.json --promote   # 确认新数字后升为基线

可挂 pre-push / CI。退出码：
    0 = 通过
    1 = 有指标回归
    2 = **无法判定**（文件/用法错误，或任一边的上游处于降级态）

退出码 2 的后一种情况很重要：上游被掐断时测到的指标全是噪声，拿它去比
要么假阴（基线本身就是零分，怎么跑都「优于基线」）要么假阳（代码没动却报回归）。
**无效的测量不应该被报成通过或失败**，所以这种情况不判定，直接让人先去看环境。

## 为什么只卡这两个指标

计划定的门禁口径是 `nDCG@10` 与 `recall@50`——它们分别守住漏斗的两端：
recall@50 掉了说明**召回上限**被破坏（引擎挂了、去重误杀、窗口缩小），
nDCG@10 掉了说明**排序体感**被破坏（精排退化、融合权重失衡）。

其余指标（MRR / 片段命中率 / p95 延迟）**只报告不卡门**：它们波动天然更大
（片段命中依赖抽取，抽取依赖对方站点当天是否反爬），拿它们卡门会让 CI 经常红，
红了也没人能判断是代码退化还是网络抖动。真出问题时它们会和前两个一起动。

## 只比非时效条目

`stable` 那一档是门禁口径：时效类查询的结果每天都在换，把它们算进来，
今天跑绿明天跑红，而两次之间代码一个字没改。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HERE = Path(__file__).resolve().parent
DEFAULT_BASELINE = HERE / "reports" / "baseline.json"

# 卡门的指标：(键, 中文名, 容忍回落幅度)
GATED: tuple[tuple[str, str, float], ...] = (
    ("ndcg_at_10", "nDCG@10", 0.03),
    ("recall_at_50", "recall@50", 0.03),
)
# 只报告不卡门的指标
WATCHED: tuple[tuple[str, str], ...] = (
    ("mrr", "MRR"),
    ("span_hit_rate", "片段命中率"),
    ("route_agreement", "多路一致率"),
    ("available_rate", "可用率"),
)
LATENCY_WATCH = ("p95_ms", "p95 延迟", 1.5)  # 慢了 1.5× 以上才提示（延迟受本机负载影响大）


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"找不到报告文件：{path}\n（先用 eval.run --json 生成）")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} 不是合法 JSON：{e}") from e
    if "configs" not in data:
        raise SystemExit(f"{path} 缺少 configs 字段，不像 eval.run 产出的报告")
    return data


def pick(data: dict[str, Any], ablate: str) -> dict[str, Any]:
    cfg = (data.get("configs") or {}).get(ablate)
    if cfg is None:
        have = ",".join(sorted(data.get("configs") or {})) or "（空）"
        raise SystemExit(f"报告里没有消融档 {ablate!r}，现有：{have}")
    return cfg


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def compare_queries(base: list[dict[str, Any]], new: list[dict[str, Any]], tol: float) -> list[str]:
    """逐条对比，把**哪几条 query 掉了**说出来——平均分从不告诉你该去查什么。"""
    by_id = {r["id"]: r for r in base}
    lines: list[str] = []
    for row in new:
        old = by_id.get(row["id"])
        if old is None:
            lines.append(f"  {row['id']:<10} 新增条目（基线里没有，不参与对比）")
            continue
        for key, label in (("ndcg", "nDCG"), ("recall", "recall"), ("mrr", "MRR")):
            delta = float(row.get(key) or 0.0) - float(old.get(key) or 0.0)
            if key != "mrr" and delta < -tol:
                lines.append(
                    f"  {row['id']:<10} {label} {old.get(key):.3f} → {row.get(key):.3f} "
                    f"（{delta:+.3f}）window {old.get('window')}→{row.get('window')}"
                )
            elif key == "mrr" and delta < -0.2:
                lines.append(f"  {row['id']:<10} {label} {old.get(key):.2f} → {row.get(key):.2f}")
    return lines


def upstream_of(data: dict[str, Any], label: str) -> tuple[bool, list[str]]:
    """读报告里的上游健康标记（eval.run 写的 upstream.degraded）。

    旧报告没这个字段 → 视为未降级（不阻断），但提醒一句：那是守卫加上之前跑的。
    """
    up = data.get("upstream")
    if up is None:
        return False, [f"{label}没有 upstream 字段（旧版报告），无法确认当时上游是否健康"]
    return bool(up.get("degraded")), list(up.get("reasons") or [])


def main() -> int:
    ap = argparse.ArgumentParser(description="与基线对比，回归超阈值 exit 1")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--report", required=True, help="eval.run --json 产出的新报告")
    ap.add_argument("--ablate", default="none", help="比哪一档消融（默认 none = 全流水线）")
    ap.add_argument("--tolerance", type=float, default=0.03,
                    help="nDCG@10 / recall@50 允许的回落幅度（默认 0.03，计划定的门禁值）")
    ap.add_argument("--promote", action="store_true",
                    help="通过后把 --report 复制成基线（省得手工 cp 忘了）")
    args = ap.parse_args()

    base_path, report_path = Path(args.baseline), Path(args.report)
    base, new = load(base_path), load(report_path)
    bcfg, ncfg = pick(base, args.ablate), pick(new, args.ablate)
    bsum, nsum = bcfg.get("stable") or {}, ncfg.get("stable") or {}
    if not bsum or not nsum:
        print("报告缺少 stable（非时效）汇总，无法按门禁口径对比", file=sys.stderr)
        return 2

    print(f"对比消融档：{args.ablate}（stages={','.join(ncfg.get('stages') or [])}）")
    b_at, b_n = base.get("generated_at", "?"), base.get("query_count", "?")
    n_at, n_n = new.get("generated_at", "?"), new.get("query_count", "?")
    print(f"基线：{base_path}  {b_at}  {b_n} 条")
    print(f"新报告：{report_path}  {n_at}  {n_n} 条")
    if b_n != n_n:
        print("⚠ 两边金标集条数不同：指标可比性已受损，结论仅供参考")

    # 上游降级 → 不判定。这必须在算指标之前拦下来：
    # 否则一个零分基线会让之后每次跑分都「通过」，门禁无声失效
    base_bad, base_reasons = upstream_of(base, "基线")
    new_bad, new_reasons = upstream_of(new, "新报告")
    if base_bad or new_bad:
        print("\n无法判定：上游搜索引擎处于降级态，本轮指标不反映流水线质量。")
        for r in base_reasons + new_reasons:
            print(f"  - {r}")
        if base_bad:
            print("  基线本身不可信 → 等引擎恢复后重跑 eval.run 并 --promote 一份新基线。")
        if new_bad:
            print("  新报告不可信 → 等引擎退避期过去（通常 1~10 分钟）再跑。")
        return 2
    for note in base_reasons + new_reasons:
        print(f"⚠ {note}")

    print(f"\n{'指标':<14}{'基线':>10}{'本次':>10}{'差值':>10}   判定")
    print("-" * 62)
    failures: list[str] = []
    for key, label, default_tol in GATED:
        tol = args.tolerance if default_tol == 0.03 else default_tol
        b, n = bsum.get(key), nsum.get(key)
        if b is None or n is None:
            print(f"{label:<14}{_fmt(b):>10}{_fmt(n):>10}{'—':>10}   跳过（缺数据）")
            continue
        delta = n - b
        ok = delta >= -tol
        mark = "通过" if ok else f"回归（容忍 -{tol}）"
        print(f"{label:<14}{b:>10.4f}{n:>10.4f}{delta:>+10.4f}   {mark}")
        if not ok:
            failures.append(f"{label} {b:.4f} → {n:.4f}（{delta:+.4f}，超出 -{tol}）")

    for key, label in WATCHED:
        b, n = bsum.get(key), nsum.get(key)
        if b is None or n is None:
            continue
        print(f"{label:<14}{b:>10.4f}{n:>10.4f}{n - b:>+10.4f}   仅报告")
    key, label, ratio = LATENCY_WATCH
    b, n = bsum.get(key) or 0, nsum.get(key) or 0
    if b and n:
        flag = "仅报告" if n <= b * ratio else f"变慢 {n / b:.1f}×（仅报告）"
        print(f"{label:<14}{b:>10}{n:>10}{n - b:>+10}   {flag}")

    # 组件可用性：基线跑的时候三件套都在、这次少了 → 指标下降是环境问题不是代码退化，
    # 这种情况必须说出来，否则有人会去改流水线参数「修」一个网络问题
    bc, nc = base.get("health_components") or {}, new.get("health_components") or {}
    lost = [k for k, v in bc.items() if v and not nc.get(k)]
    if lost:
        print(f"\n⚠ 本次不可用而基线可用的组件：{','.join(lost)}"
              " —— 指标回落很可能来自降级而非代码退化，先修环境再判门禁")

    drops = compare_queries(bcfg.get("queries") or [], ncfg.get("queries") or [], args.tolerance)
    if drops:
        print(f"\n逐条回落（阈值 -{args.tolerance}）：")
        print("\n".join(drops[:20]))
        if len(drops) > 20:
            print(f"  …另有 {len(drops) - 20} 条")

    if failures:
        print("\n门禁未通过：")
        for f in failures:
            print(f"  ✗ {f}")
        print("排查顺序：先看上面的逐条回落与不可用组件，再用 "
              "data/_scratch/diag.py \"<query>\" 打出那一条的各路排名与精排分。")
        return 1

    print("\n门禁通过。")
    if args.promote:
        if report_path.resolve() == base_path.resolve():
            print("（--report 与 --baseline 是同一个文件，无需提升）")
        else:
            base_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report_path, base_path)
            print(f"已把 {report_path} 提升为基线 {base_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
