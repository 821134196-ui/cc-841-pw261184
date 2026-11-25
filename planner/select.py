"""测试选择:把失效制品转成覆盖义务,在成本预算内选测试,剩下的算成具体风险。

义务模型(每个失效的 (模块, 平台) 产生):
  * 架构义务 (module, platform):只能由 arch/e2e 测试清偿;
  * 契约义务 (module, contract):模块显式声明的每个契约一条,只能由
    contract 测试清偿——通用测试的覆盖范围以声明的契约为界,不得外推。

预算耗尽或根本没有候选测试的义务 => 风险条目,带具体溯源与缺口数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .graph import Trace
from .model import Repo, Test, glob_to_regex


@dataclass(frozen=True)
class Obligation:
    """一条必须被测试清偿的覆盖义务。"""

    type: str            # "arch" | "contract"
    module: str
    platform: Optional[str] = None   # arch 义务有
    contract: Optional[str] = None   # contract 义务有
    direct: bool = False             # 模块是否直接变更(否则为传递影响)

    @property
    def key(self) -> tuple:
        return (self.type, self.module, self.platform, self.contract)

    def describe(self) -> str:
        if self.type == "arch":
            return f"{self.module} @ {self.platform} 的架构专用验证"
        return f"{self.module} 声明的契约 {self.contract}"


@dataclass
class Risk:
    id: str
    kind: str            # "no-test" | "budget"
    severity: str        # critical | high | medium
    obligation: Obligation
    summary: str
    detail: str
    trace: Optional[Trace] = None
    needed_cost: Optional[int] = None     # budget 风险:最便宜候选的成本
    remaining_budget: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "obligation": {
                "type": self.obligation.type,
                "module": self.obligation.module,
                "platform": self.obligation.platform,
                "contract": self.obligation.contract,
                "direct": self.obligation.direct,
            },
            "summary": self.summary,
            "detail": self.detail,
            "trace": self.trace.to_dict() if self.trace else None,
            "needed_cost": self.needed_cost,
            "remaining_budget": self.remaining_budget,
        }


def build_obligations(repo: Repo, affected: dict, changed: list) -> list:
    """从失效的 (模块, 平台) 集合生成义务清单(去重、定序)。"""
    changed_set = set(changed)
    obligations = {}
    for (mod_name, platform) in affected:
        mod = repo.modules[mod_name]
        direct = mod_name in changed_set
        ob = Obligation("arch", mod_name, platform=platform, direct=direct)
        obligations[ob.key] = ob
        for c in mod.contracts:
            cob = Obligation("contract", mod_name, contract=c, direct=direct)
            obligations.setdefault(cob.key, cob)
    return list(obligations.values())


def _test_modules(repo: Repo, test: Test) -> set:
    """arch/e2e 测试覆盖的模块集合(展开通配符)。"""
    out = set()
    names = repo.modules.keys()
    for m in test.covers_modules:
        if m.name:
            out.add(m.name)
        else:
            rx = glob_to_regex(m.pattern)
            out.update(n for n in names if rx.match(n))
    return out


def candidates_for(repo: Repo, ob: Obligation) -> list:
    """义务的候选测试——类型匹配是硬规则,通用测试不能越界。"""
    out = []
    for t in repo.tests:
        if ob.type == "contract":
            if t.is_generic and ob.contract in t.covers_contracts:
                out.append(t)
        else:  # arch
            if not t.is_generic and ob.module in _test_modules(repo, t) and ob.platform in t.platforms:
                out.append(t)
    return out


def _priority(ob: Obligation) -> int:
    """清偿顺序:直接变更的架构义务最优先,传递契约义务最后。"""
    if ob.type == "arch":
        return 0 if ob.direct else 1
    return 2 if ob.direct else 3


def _severity(ob: Obligation) -> str:
    if ob.type == "arch":
        return "critical" if ob.direct else "high"
    return "high" if ob.direct else "medium"


def select_tests(repo: Repo, obligations: list, affected: dict, budget: Optional[int]) -> dict:
    """贪心集合覆盖:按义务优先级清偿,候选按"新覆盖义务多、便宜"排序。"""
    traces_by_module = {}
    for (mod_name, platform), trace in affected.items():
        traces_by_module.setdefault(mod_name, {})[platform] = trace

    def trace_for(ob: Obligation) -> Optional[Trace]:
        per_platform = traces_by_module.get(ob.module, {})
        if ob.type == "arch":
            return per_platform.get(ob.platform)
        # 契约义务与平台无关,取该模块任一平台的溯源链
        return next(iter(per_platform.values()), None)

    # 每个测试能清偿哪些义务(只算真实存在的义务)
    coverage = {t.name: set() for t in repo.tests}
    for ob in obligations:
        for t in candidates_for(repo, ob):
            coverage[t.name].add(ob.key)

    ordered = sorted(
        obligations,
        key=lambda o: (_priority(o), o.module, o.platform or "", o.contract or ""),
    )

    selected = []          # list[Test]
    selected_names = set()
    reasons = {}           # test name -> list[Obligation]
    satisfied = set()
    risks = []
    total_cost = 0

    for ob in ordered:
        if ob.key in satisfied:
            continue
        cands = sorted(
            candidates_for(repo, ob),
            key=lambda t: (
                0 if t.name in selected_names else 1,
                -len(coverage[t.name] - satisfied),
                t.cost,
                t.name,
            ),
        )
        chosen = None
        for t in cands:
            if t.name in selected_names:
                chosen = t
                break
            if budget is None or total_cost + t.cost <= budget:
                chosen = t
                break
        if chosen is None:
            trace = trace_for(ob)
            if cands:
                cheapest = min(cands, key=lambda t: (t.cost, t.name))
                remaining = None if budget is None else budget - total_cost
                risks.append(Risk(
                    id=f"R{len(risks) + 1}",
                    kind="budget",
                    severity=_severity(ob),
                    obligation=ob,
                    summary=f"预算不足:{ob.describe()} 无法安排",
                    detail=_budget_detail(ob, cheapest, remaining, trace),
                    trace=trace,
                    needed_cost=cheapest.cost,
                    remaining_budget=remaining,
                ))
            else:
                risks.append(Risk(
                    id=f"R{len(risks) + 1}",
                    kind="no-test",
                    severity=_severity(ob),
                    obligation=ob,
                    summary=f"无可用测试:{ob.describe()} 没有任何候选",
                    detail=_no_test_detail(repo, ob, trace),
                    trace=trace,
                ))
            continue

        if chosen.name not in selected_names:
            selected.append(chosen)
            selected_names.add(chosen.name)
            total_cost += chosen.cost
        newly = coverage[chosen.name] - satisfied
        satisfied |= coverage[chosen.name]
        reasons.setdefault(chosen.name, [])
        for key in sorted(newly):
            o = next(o for o in obligations if o.key == key)
            reasons[chosen.name].append(o)

    # 每个入选测试的完整理由:义务 + 义务的溯源链
    test_reasons = []
    for t in selected:
        test_reasons.append({
            "test": {
                "name": t.name,
                "kind": t.kind,
                "cost": t.cost,
                "platforms": t.platforms,
                "covers_contracts": t.covers_contracts,
                "generic": t.is_generic,
            },
            "obligations": [
                {
                    "describe": ob.describe(),
                    "type": ob.type,
                    "module": ob.module,
                    "platform": ob.platform,
                    "contract": ob.contract,
                    "direct": ob.direct,
                    "trace": trace_for(ob).to_dict() if trace_for(ob) else None,
                }
                for ob in sorted(
                    reasons.get(t.name, []),
                    key=lambda o: (o.module, o.platform or "", o.contract or ""),
                )
            ],
        })

    return {
        "selected": test_reasons,
        "total_cost": total_cost,
        "budget": budget,
        "remaining": None if budget is None else budget - total_cost,
        "risks": risks,
        "obligations_total": len(obligations),
        "obligations_covered": len(satisfied),
    }


def _budget_detail(ob: Obligation, cheapest: Test, remaining, trace: Optional[Trace]) -> str:
    src = "直接变更" if ob.direct else "受变更传递影响"
    return (
        f"模块 {ob.module} {src},制品需要{ob.describe()};"
        f"最便宜的候选测试 {cheapest.name} 需 {cheapest.cost} 分钟,"
        f"但剩余预算只有 {remaining} 分钟。"
        f"该项验证缺失将随本次发布上线,建议追加预算或单独补跑 {cheapest.name}。"
    )


def _no_test_detail(repo: Repo, ob: Obligation, trace: Optional[Trace]) -> str:
    mod = repo.modules[ob.module]
    src = "直接变更" if ob.direct else "受变更传递影响"
    if ob.type == "arch":
        generic_note = ""
        if mod.contracts:
            generic_note = (
                f"该模块声明的契约 [{', '.join(mod.contracts)}] 可由通用契约测试覆盖,"
                f"但契约之外的 {ob.platform} 平台专属行为没有任何测试验证——"
                f"通用测试不能替代架构专用用例。"
            )
        else:
            generic_note = "该模块未声明任何契约,连通用契约测试都无法覆盖它。"
        return (
            f"模块 {ob.module} {src},{ob.platform} 平台制品已失效,"
            f"但没有任何 arch/e2e 测试覆盖 ({ob.module}, {ob.platform})。{generic_note}"
            f"需要补一个覆盖该模块该平台用例的架构/端到端测试。"
        )
    return (
        f"模块 {ob.module} {src},其声明的契约 {ob.contract} 没有任何契约测试覆盖。"
        f"请为该契约补充通用契约测试,或从模块的 contracts 中移除该声明。"
    )
