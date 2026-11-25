"""依赖图:通配符解析、循环依赖检测、失效传播(每一步都留溯源链)。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .model import Repo, glob_to_regex


@dataclass(frozen=True)
class Edge:
    """一条已解析的依赖边:consumer 依赖 dependency。"""

    consumer: str
    dependency: str
    via: str  # "explicit" | "wildcard"
    pattern: Optional[str] = None       # via=wildcard 时的原始 pattern
    platforms: Optional[frozenset] = None  # None = 不限平台

    def describe(self) -> str:
        if self.via == "wildcard":
            s = f'经通配符 "{self.pattern}" 依赖 {self.dependency}'
        else:
            s = f"直接依赖 {self.dependency}"
        if self.platforms is not None:
            s += f"(仅平台 {','.join(sorted(self.platforms))})"
        return s


def resolve_edges(repo: Repo) -> list:
    """把显式依赖和通配符依赖都解析成具体边。通配符每次运行重新展开。"""
    edges = []
    names = sorted(repo.modules)
    for mod in repo.modules.values():
        for dep in mod.deps:
            if dep.name:
                edges.append(Edge(mod.name, dep.name, "explicit", None, dep.platforms))
            else:
                rx = glob_to_regex(dep.pattern)
                for target in names:
                    if target != mod.name and rx.match(target):
                        edges.append(Edge(mod.name, target, "wildcard", dep.pattern, dep.platforms))
    return edges


def find_cycles(edges: list) -> list:
    """Tarjan SCC(迭代实现)。只返回有意义的环:>1 个成员,或自环。

    返回 [{"id": int, "members": [...]}],成员按名字排序。
    """
    adj = {}
    for e in edges:
        adj.setdefault(e.consumer, set()).add(e.dependency)
        adj.setdefault(e.dependency, set())

    index_of, lowlink, on_stack = {}, {}, set()
    stack, sccs = [], []
    counter = [0]

    for root in sorted(adj):
        if root in index_of:
            continue
        work = [(root, iter(sorted(adj[root])))]
        index_of[root] = lowlink[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = lowlink[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(adj.get(nxt, ())))))
                    advanced = True
                    break
                elif nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                scc = []
                while True:
                    m = stack.pop()
                    on_stack.discard(m)
                    scc.append(m)
                    if m == node:
                        break
                if len(scc) > 1 or node in adj.get(node, ()):
                    sccs.append(sorted(scc))

    return [{"id": i + 1, "members": s} for i, s in enumerate(sorted(sccs, key=lambda s: s[0]))]


@dataclass
class Trace:
    """一个 (模块, 平台) 为何失效:从变更源到它的传播路径。"""

    steps: list = field(default_factory=list)
    # steps: [{"module", "note", "edge": Edge|None}]
    cycle_note: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "steps": [
                {
                    "module": s["module"],
                    "note": s["note"],
                    "edge": (
                        {
                            "via": s["edge"].via,
                            "pattern": s["edge"].pattern,
                            "platforms": sorted(s["edge"].platforms) if s["edge"].platforms else None,
                            "describe": s["edge"].describe(),
                        }
                        if s["edge"]
                        else None
                    ),
                }
                for s in self.steps
            ],
            "cycle_note": self.cycle_note,
        }


def map_changes(repo: Repo, change_lines: list) -> tuple:
    """把变更行(模块名或文件路径)映射到模块。返回 (changed_names, unmapped)。"""
    changed, unmapped = set(), []
    for raw in change_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in repo.modules:
            changed.add(line)
            continue
        hit = None
        for mod in repo.modules.values():
            if any(glob_to_regex(p).match(line) for p in mod.paths):
                hit = mod.name
                break
        if hit:
            changed.add(hit)
        else:
            unmapped.append(line)
    return sorted(changed), unmapped


def propagate(repo: Repo, edges: list, cycles: list, changed: list) -> dict:
    """以 (模块, 平台) 为状态做 BFS,返回 {(module, platform): Trace}。

    传播规则:
      * 变更模块的全部平台制品失效;
      * 若 (m, p) 失效,且存在边 consumer->m 适用于平台 p,且 consumer 为 p 构建,
        则 (consumer, p) 失效;
      * 平台限定边只沿限定平台传播——平台专属构建不会误伤其它平台;
      * 环上的模块互为依赖,BFS 天然收敛;环成员在 Trace 上额外标注。
    """
    cycle_of = {}
    cycle_note_of = {}
    for c in cycles:
        note = f"循环依赖组 #{c['id']}({' ↔ '.join(c['members'])}):任一成员变更,全组制品失效"
        for m in c["members"]:
            cycle_of[m] = c["id"]
            cycle_note_of[m] = note

    # 反向邻接:dependency -> [(consumer, edge)]
    rev = {}
    for e in edges:
        rev.setdefault(e.dependency, []).append((e.consumer, e))

    affected = {}
    queue = deque()
    for name in changed:
        mod = repo.modules[name]
        for p in mod.platforms:
            key = (name, p)
            if key in affected:
                continue
            trace = Trace(steps=[{"module": name, "note": "变更源", "edge": None}])
            trace.cycle_note = cycle_note_of.get(name)
            affected[key] = trace
            queue.append(key)

    while queue:
        mod_name, platform = queue.popleft()
        for consumer_name, edge in rev.get(mod_name, ()):
            if edge.platforms is not None and platform not in edge.platforms:
                continue  # 平台限定边:该平台不在边上,不传播
            consumer = repo.modules[consumer_name]
            if platform not in consumer.platforms:
                continue  # 消费者不为该平台构建制品
            key = (consumer_name, platform)
            if key in affected:
                continue
            parent = affected[(mod_name, platform)]
            trace = Trace(steps=parent.steps + [{
                "module": consumer_name,
                "note": edge.describe(),
                "edge": edge,
            }])
            trace.cycle_note = cycle_note_of.get(consumer_name)
            affected[key] = trace
            queue.append(key)

    return affected
