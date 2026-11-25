"""planner 的单元测试:通配符、循环依赖、平台传播、覆盖规则、预算与溯源。"""

import json
import tempfile
import unittest
from pathlib import Path

from planner.cli import build_plan
from planner.graph import find_cycles, map_changes, propagate, resolve_edges
from planner.model import SpecError, load_repo, validate_repo, glob_to_regex
from planner.select import build_obligations, candidates_for, select_tests

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo"


def load_demo():
    return load_repo(DEMO / "graph")


class TestGlob(unittest.TestCase):
    def test_star_does_not_cross_slash(self):
        self.assertTrue(glob_to_regex("feature/*").match("feature/player"))
        self.assertFalse(glob_to_regex("feature/*").match("feature/player/sub"))

    def test_double_star_crosses_slash(self):
        self.assertTrue(glob_to_regex("libs/crypto/**").match("libs/crypto/a/b.py"))
        self.assertFalse(glob_to_regex("libs/crypto/*").match("libs/crypto/a/b.py"))


class TestGraph(unittest.TestCase):
    def setUp(self):
        self.repo = load_demo()
        self.edges = resolve_edges(self.repo)

    def test_wildcard_resolves_to_concrete_edges(self):
        wild = [e for e in self.edges if e.via == "wildcard"]
        self.assertTrue(wild)
        self.assertTrue(all(e.consumer == "app/shell" for e in wild))
        self.assertEqual(
            {e.dependency for e in wild},
            {"feature/player", "feature/playlist", "feature/search"},
        )
        # 每条通配符边都记得自己的 pattern,供报告解释
        self.assertTrue(all(e.pattern == "feature/*" for e in wild))

    def test_cycle_detected(self):
        cycles = find_cycles(self.edges)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["members"], ["feature/player", "feature/playlist"])

    def test_platform_scoped_edge_only_propagates_on_that_platform(self):
        # keystore-ios 变更:只能让 app/shell 的 ios 制品失效,不能波及 web/android
        affected = propagate(self.repo, self.edges, find_cycles(self.edges),
                             ["platform/keystore-ios"])
        shells = {p for (m, p) in affected if m == "app/shell"}
        self.assertEqual(shells, {"ios"})
        # 也不应逆流到 core/crypto
        self.assertFalse(any(m == "core/crypto" for (m, _) in affected))

    def test_change_in_cycle_invalidates_whole_group(self):
        cycles = find_cycles(self.edges)
        affected = propagate(self.repo, self.edges, cycles, ["feature/player"])
        for plat in ("ios", "android", "web"):
            self.assertIn(("feature/playlist", plat), affected)
        # 环成员的溯源链带循环说明
        self.assertIn("循环依赖组", affected[("feature/playlist", "ios")].cycle_note)

    def test_unaffected_modules_stay_out(self):
        affected = propagate(self.repo, self.edges, find_cycles(self.edges),
                             ["core/crypto"])
        hit = {m for (m, _) in affected}
        self.assertIn("app/shell", hit)                    # 显式依赖 crypto
        self.assertIn("platform/keystore-ios", hit)        # 依赖 crypto
        self.assertNotIn("app/wear", hit)                  # 只依赖 core/base
        self.assertNotIn("feature/search", hit)            # 只依赖 core/net
        self.assertNotIn("core/net", hit)

    def test_trace_ends_at_change_source(self):
        affected = propagate(self.repo, self.edges, find_cycles(self.edges),
                             ["core/crypto"])
        trace = affected[("app/shell", "web")]
        self.assertEqual(trace.steps[0]["module"], "core/crypto")
        self.assertEqual(trace.steps[-1]["module"], "app/shell")

    def test_map_changes_by_name_and_path(self):
        changed, unmapped = map_changes(
            self.repo, ["core/crypto", "features/player/a.py", "README.md", "# 注释"])
        self.assertEqual(changed, ["core/crypto", "feature/player"])
        self.assertEqual(unmapped, ["README.md"])


class TestSelection(unittest.TestCase):
    def setUp(self):
        self.repo = load_demo()
        self.edges = resolve_edges(self.repo)
        self.cycles = find_cycles(self.edges)
        self.changed = ["core/crypto", "feature/player"]
        self.affected = propagate(self.repo, self.edges, self.cycles, self.changed)
        self.obligations = build_obligations(self.repo, self.affected, self.changed)

    def test_generic_test_cannot_cover_arch_obligation(self):
        # (core/crypto, web) 没有 web 架构测试;契约测试存在也不许顶替
        web_arch = next(o for o in self.obligations
                        if o.type == "arch" and o.module == "core/crypto"
                        and o.platform == "web")
        self.assertEqual(candidates_for(self.repo, web_arch), [])
        result = select_tests(self.repo, self.obligations, self.affected, budget=None)
        kinds = {(r.obligation.module, r.obligation.platform): r.kind
                 for r in result["risks"]}
        self.assertEqual(kinds[("core/crypto", "web")], "no-test")

    def test_contract_obligation_only_by_contract_test(self):
        ob = next(o for o in self.obligations if o.type == "contract")
        cands = candidates_for(self.repo, ob)
        self.assertEqual([t.name for t in cands], ["crypto-contract"])

    def test_unlimited_budget_covers_everything_coverable(self):
        result = select_tests(self.repo, self.obligations, self.affected, budget=None)
        self.assertEqual(result["remaining"], None)
        # 唯一剩下的风险是 (core/crypto, web) 的无测试风险
        self.assertEqual([r.kind for r in result["risks"]], ["no-test"])
        names = {e["test"]["name"] for e in result["selected"]}
        self.assertIn("player-playlist-e2e", names)   # 一个 e2e 覆盖循环组 6 项义务
        self.assertNotIn("net-contract", names)       # core/net 未受影响,不入选

    def test_budget_produces_specific_risks(self):
        result = select_tests(self.repo, self.obligations, self.affected, budget=300)
        self.assertLessEqual(result["total_cost"], 300)
        budget_risks = [r for r in result["risks"] if r.kind == "budget"]
        self.assertTrue(budget_risks)
        for r in budget_risks:
            self.assertIsNotNone(r.needed_cost)
            self.assertIsNotNone(r.remaining_budget)
            self.assertGreater(r.needed_cost, r.remaining_budget)
            self.assertIn("分钟", r.detail)  # 风险必须带具体数字

    def test_every_selected_test_has_traceable_reasons(self):
        result = select_tests(self.repo, self.obligations, self.affected, budget=300)
        self.assertTrue(result["selected"])
        for entry in result["selected"]:
            self.assertTrue(entry["obligations"], entry["test"]["name"])
            for ob in entry["obligations"]:
                steps = ob["trace"]["steps"]
                self.assertEqual(steps[0]["note"], "变更源")
                self.assertIn(steps[0]["module"], self.changed)

    def test_priority_direct_arch_before_transitive(self):
        # 预算只够一个测试时,先保直接变更的架构义务
        result = select_tests(self.repo, self.obligations, self.affected, budget=45)
        first = result["selected"][0]["test"]["name"]
        self.assertIn(first, ("crypto-arch-android", "crypto-arch-ios"))


class TestPlan(unittest.TestCase):
    def test_demo_plan_end_to_end(self):
        plan = build_plan(DEMO / "graph", DEMO / "changes.txt", budget=300)
        self.assertEqual(plan["changes"]["modules"], ["core/crypto", "feature/player"])
        self.assertEqual(plan["changes"]["unmapped"], ["docs/release-notes.md"])
        self.assertEqual(len(plan["cycles"]), 1)
        self.assertEqual(len(plan["wildcard_edges"]), 1)
        self.assertEqual(len(plan["wildcard_edges"][0]["matched"]), 3)
        # 14 个失效制品:crypto×3 + keystore×2 + player×3 + playlist×3 + shell×3
        self.assertEqual(plan["stats"]["invalidated"], 14)
        self.assertEqual(plan["tests"]["total_cost"], 300)
        kinds = sorted(r["kind"] for r in plan["risks"])
        self.assertEqual(kinds, ["budget", "budget", "no-test"])
        # 每个失效制品都有完整溯源
        for inv in plan["invalidated"]:
            self.assertEqual(inv["trace"]["steps"][0]["note"], "变更源")

    def test_unmapped_all_mode_invalidates_everything(self):
        plan = build_plan(DEMO / "graph", DEMO / "changes.txt", budget=None,
                          unmapped_mode="all")
        # 所有模块的所有平台制品都失效
        self.assertEqual(plan["stats"]["invalidated"], 24)  # 7×3 平台 + 3×1 平台

    def test_unknown_dependency_is_a_hard_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "modules.json").write_text(json.dumps({
                "modules": [{"name": "a", "platforms": ["ios"], "deps": ["ghost"]}]
            }), encoding="utf-8")
            (p / "tests.json").write_text(json.dumps({"tests": []}), encoding="utf-8")
            with self.assertRaises(SpecError):
                validate_repo(load_repo(p))

    def test_contract_test_must_declare_contracts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "modules.json").write_text(json.dumps({"modules": []}), encoding="utf-8")
            (p / "tests.json").write_text(json.dumps({
                "tests": [{"name": "t", "kind": "contract", "cost": 1}]
            }), encoding="utf-8")
            with self.assertRaises(SpecError):
                load_repo(p)


if __name__ == "__main__":
    unittest.main()
