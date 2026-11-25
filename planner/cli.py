"""命令行入口:

  python3 -m planner validate --graph examples/demo/graph
  python3 -m planner plan --graph examples/demo/graph \
      --changes examples/demo/changes.txt --budget 300 --out examples/demo/out

退出码:0 正常;1 输入不合法;2 计划存在未覆盖风险(仅 --fail-on-risk 时)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .graph import find_cycles, map_changes, propagate, resolve_edges
from .model import SpecError, load_repo, validate_repo
from .report import write_outputs
from .select import build_obligations, select_tests


def _read_changes(path) -> list:
    return Path(path).read_text(encoding="utf-8").splitlines()


def build_plan(graph_dir, changes_file, budget, unmapped_mode="warn") -> dict:
    repo = load_repo(graph_dir)
    warnings = validate_repo(repo)

    changed, unmapped = map_changes(repo, _read_changes(changes_file))
    if unmapped and unmapped_mode == "all":
        # 保守模式:无法归属的变更视为全量失效
        changed = sorted(repo.modules)
        warnings.append(
            f"有 {len(unmapped)} 个变更无法映射到模块,按 --unmapped=all 策略全量失效"
        )
    elif unmapped:
        warnings.append(f"有 {len(unmapped)} 个变更无法映射到任何模块,已忽略(见报告)")

    edges = resolve_edges(repo)
    cycles = find_cycles(edges)
    affected = propagate(repo, edges, cycles, changed)
    obligations = build_obligations(repo, affected, changed)
    selection = select_tests(repo, obligations, affected, budget)

    # 通配符解析结果(按 消费者×模式 聚合,便于报告展示)
    wild = {}
    for e in edges:
        if e.via == "wildcard":
            key = (e.consumer, e.pattern, e.platforms)
            wild.setdefault(key, []).append(e.dependency)
    wildcard_edges = [
        {
            "consumer": c,
            "pattern": p,
            "matched": sorted(m),
            "platforms": sorted(plats) if plats else None,
        }
        for (c, p, plats), m in sorted(wild.items())
    ]

    changed_set = set(changed)
    invalidated = [
        {
            "artifact": f"{mod}:{plat}",
            "module": mod,
            "platform": plat,
            "direct": mod in changed_set,
            "trace": trace.to_dict(),
        }
        for (mod, plat), trace in sorted(affected.items())
    ]

    risks = [r.to_dict() for r in selection["risks"]]
    tests = {
        "selected": selection["selected"],
        "total_cost": selection["total_cost"],
        "budget": budget,
        "remaining": selection["remaining"],
        "obligations_total": selection["obligations_total"],
        "obligations_covered": selection["obligations_covered"],
        "risks_over_budget": bool(selection["risks"]),
    }

    return {
        "version": 1,
        "tool": "pointwise-delivery",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "graph_dir": str(graph_dir),
        "changes": {"modules": changed, "unmapped": unmapped},
        "wildcard_edges": wildcard_edges,
        "cycles": [
            {"id": c["id"], "members": c["members"],
             "note": f"任一成员变更,全组 {len(c['members'])} 个模块的制品失效"}
            for c in cycles
        ],
        "invalidated": invalidated,
        "tests": tests,
        "risks": risks,
        "warnings": warnings,
        "stats": {
            "modules": len(repo.modules),
            "tests": len(repo.tests),
            "edges": len(edges),
            "changed": len(changed),
            "invalidated": len(invalidated),
            "selected_tests": len(selection["selected"]),
            "risks": len(risks),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="planner",
        description="变更影响面流水线计划器:失效制品 + 必跑测试 + 预算风险",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate", help="校验依赖图与测试清单")
    pv.add_argument("--graph", required=True, help="含 modules.json / tests.json 的目录")

    pp = sub.add_parser("plan", help="计算失效制品、必跑测试与预算风险")
    pp.add_argument("--graph", required=True)
    pp.add_argument("--changes", required=True, help="变更清单文件:每行一个模块名或文件路径")
    pp.add_argument("--budget", type=int, default=None, help="测试成本预算(分钟),缺省不限")
    pp.add_argument("--out", required=True, help="输出目录(plan.json + report.html)")
    pp.add_argument("--unmapped", choices=["warn", "all"], default="warn",
                    help="无法归属的变更:warn=警告并忽略(默认);all=保守全量失效")
    pp.add_argument("--fail-on-risk", action="store_true",
                    help="存在未覆盖风险时以退出码 2 结束(供 CI 卡点)")

    args = ap.parse_args(argv)

    try:
        if args.cmd == "validate":
            repo = load_repo(args.graph)
            warnings = validate_repo(repo)
            for w in warnings:
                print(f"警告: {w}")
            base = f"校验通过: {len(repo.modules)} 个模块,{len(repo.tests)} 个测试"
            print(f"{base},{len(warnings)} 个警告。" if warnings else f"{base},无警告。")
            return 0

        plan = build_plan(args.graph, args.changes, args.budget, args.unmapped)
        plan_path, html_path = write_outputs(plan, args.out)

        s = plan["stats"]
        print(f"变更模块 {s['changed']} → 失效制品 {s['invalidated']} "
              f"(共 {s['modules']} 个模块,{s['edges']} 条依赖边)")
        t = plan["tests"]
        budget_txt = "不限" if t["budget"] is None else f"{t['total_cost']}/{t['budget']} 分钟"
        print(f"必跑测试 {s['selected_tests']} 个,成本 {budget_txt};"
              f"覆盖义务 {t['obligations_covered']}/{t['obligations_total']}")
        for w in plan["warnings"]:
            print(f"警告: {w}")
        if plan["risks"]:
            print(f"未覆盖风险 {len(plan['risks'])} 项:")
            for r in plan["risks"]:
                print(f"  [{r['severity']}] {r['id']} {r['summary']}")
        print(f"输出: {plan_path}")
        print(f"报告: {html_path}")
        if plan["risks"] and args.fail_on_risk:
            return 2
        return 0
    except SpecError as e:
        print(f"输入不合法: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"文件不存在: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
