"""数据模型:模块、依赖、测试,以及加载与校验。

输入为两个 JSON 文件(通常由 BUILD 文件 / 包管理器清单生成,不手写):

modules.json
------------
{
  "version": 1,
  "modules": [
    {
      "name": "core/crypto",                // 唯一模块名(路径式命名)
      "paths": ["libs/crypto/**"],          // 变更文件 -> 模块 的归属 glob
      "platforms": ["ios", "android", "web"],// 该模块为哪些平台构建制品
      "contracts": ["crypto.sign"],          // 显式声明的对外契约
      "deps": [                              // 依赖,三种形态:
        "core/base",                         //   1. 显式名字
        {"pattern": "libs/*"},               //   2. 通配符(每次运行重新解析)
        {"name": "platform/x", "platforms": ["ios"]}  // 3. 平台限定边
      ]
    }
  ]
}

tests.json
----------
{
  "version": 1,
  "tests": [
    {
      "name": "crypto-contract",
      "kind": "contract",             // contract=通用契约测试 | arch=架构专用 | e2e=端到端
      "cost": 20,                     // 成本(分钟,与 --budget 同单位)
      "covers_contracts": ["crypto.sign"],   // kind=contract 必填
      "covers_modules": ["core/crypto"],     // kind=arch/e2e 必填,支持 {"pattern": ...}
      "platforms": ["ios"]                   // kind=arch/e2e 必填
    }
  ]
}

覆盖规则(本工具的硬约束):
  * 通用契约测试(contract)只能清偿"已声明契约"的义务;
  * 架构/端到端测试(arch/e2e)才能清偿 (模块, 平台) 的架构义务;
  * 反向不成立、交叉也不成立——义务类型与测试类型必须匹配。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# glob:`*` 不跨目录,`**` 跨目录(刻意不用 fnmatch——它的 `*` 会吞掉 `/`)
# ---------------------------------------------------------------------------

def glob_to_regex(pattern: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def match_any(patterns: Iterable[str], value: str) -> bool:
    return any(glob_to_regex(p).match(value) for p in patterns)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Matcher:
    """一个引用:显式名字或通配符 pattern,可带平台限定。"""

    name: Optional[str] = None
    pattern: Optional[str] = None
    platforms: Optional[frozenset] = None  # None = 不限平台

    def describe(self) -> str:
        target = self.name if self.name else f'通配符 "{self.pattern}"'
        if self.platforms is not None:
            return f"{target}(仅 {','.join(sorted(self.platforms))})"
        return target


@dataclass
class Module:
    name: str
    paths: list = field(default_factory=list)
    platforms: list = field(default_factory=list)
    contracts: list = field(default_factory=list)
    deps: list = field(default_factory=list)  # list[Matcher]

    def artifact_ids(self) -> list:
        """制品 = 模块 × 其构建平台。"""
        return [f"{self.name}:{p}" for p in self.platforms]


@dataclass
class Test:
    name: str
    kind: str  # contract | arch | e2e
    cost: int
    covers_contracts: list = field(default_factory=list)
    covers_modules: list = field(default_factory=list)  # list[Matcher]
    platforms: list = field(default_factory=list)

    @property
    def is_generic(self) -> bool:
        """通用测试:只能覆盖显式声明的契约。"""
        return self.kind == "contract"


@dataclass
class Repo:
    modules: dict = field(default_factory=dict)  # name -> Module
    tests: list = field(default_factory=list)    # list[Test]


class SpecError(ValueError):
    """输入文件不合法。"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _parse_matcher(raw, where: str) -> Matcher:
    if isinstance(raw, str):
        return Matcher(name=raw)
    if isinstance(raw, dict):
        name = raw.get("name")
        pattern = raw.get("pattern")
        platforms = raw.get("platforms")
        if bool(name) == bool(pattern):
            raise SpecError(f"{where}: 必须且只能给 name 或 pattern 之一")
        if platforms is not None and not isinstance(platforms, list):
            raise SpecError(f"{where}: platforms 必须是数组")
        return Matcher(
            name=name,
            pattern=pattern,
            platforms=frozenset(platforms) if platforms is not None else None,
        )
    raise SpecError(f"{where}: 依赖项必须是字符串或对象,得到 {type(raw).__name__}")


def _parse_module(raw: dict) -> Module:
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise SpecError(f"模块缺少合法 name: {raw!r}")
    platforms = raw.get("platforms") or []
    if not platforms:
        raise SpecError(f"模块 {name}: platforms 不能为空(至少构建一个平台)")
    return Module(
        name=name,
        paths=list(raw.get("paths") or []),
        platforms=list(platforms),
        contracts=list(raw.get("contracts") or []),
        deps=[_parse_matcher(d, f"模块 {name} 的 deps[{i}]") for i, d in enumerate(raw.get("deps") or [])],
    )


def _parse_test(raw: dict) -> Test:
    name = raw.get("name")
    kind = raw.get("kind")
    if not name:
        raise SpecError(f"测试缺少 name: {raw!r}")
    if kind not in ("contract", "arch", "e2e"):
        raise SpecError(f"测试 {name}: kind 必须是 contract|arch|e2e,得到 {kind!r}")
    cost = raw.get("cost")
    if not isinstance(cost, int) or cost <= 0:
        raise SpecError(f"测试 {name}: cost 必须是正整数(分钟)")
    t = Test(
        name=name,
        kind=kind,
        cost=cost,
        covers_contracts=list(raw.get("covers_contracts") or []),
        covers_modules=[
            _parse_matcher(m, f"测试 {name} 的 covers_modules[{i}]")
            for i, m in enumerate(raw.get("covers_modules") or [])
        ],
        platforms=list(raw.get("platforms") or []),
    )
    if t.is_generic:
        if not t.covers_contracts:
            raise SpecError(f"契约测试 {name}: covers_contracts 不能为空")
    else:
        if not t.covers_modules or not t.platforms:
            raise SpecError(f"测试 {name}({kind}): covers_modules 与 platforms 均不能为空")
    return t


def load_repo(graph_dir) -> "Repo":
    graph_dir = Path(graph_dir)
    modules_file = graph_dir / "modules.json"
    tests_file = graph_dir / "tests.json"
    for f in (modules_file, tests_file):
        if not f.is_file():
            raise SpecError(f"缺少文件: {f}")
    modules_raw = json.loads(modules_file.read_text(encoding="utf-8"))
    tests_raw = json.loads(tests_file.read_text(encoding="utf-8"))

    repo = Repo()
    for m in modules_raw.get("modules", []):
        mod = _parse_module(m)
        if mod.name in repo.modules:
            raise SpecError(f"模块名重复: {mod.name}")
        repo.modules[mod.name] = mod
    seen_tests = set()
    for t in tests_raw.get("tests", []):
        test = _parse_test(t)
        if test.name in seen_tests:
            raise SpecError(f"测试名重复: {test.name}")
        seen_tests.add(test.name)
        repo.tests.append(test)
    return repo


# ---------------------------------------------------------------------------
# 校验(加载后调用;错误抛 SpecError,警告返回给调用方展示)
# ---------------------------------------------------------------------------

def validate_repo(repo: Repo) -> list:
    """返回警告列表;硬错误直接抛 SpecError。"""
    warnings = []
    names = set(repo.modules)

    for mod in repo.modules.values():
        for dep in mod.deps:
            if dep.name and dep.name not in names:
                raise SpecError(f"模块 {mod.name} 依赖了不存在的模块: {dep.name}")
            if dep.pattern:
                if not any(glob_to_regex(dep.pattern).match(n) for n in names):
                    warnings.append(f'模块 {mod.name} 的通配符 "{dep.pattern}" 没有匹配到任何模块')
            if dep.platforms is not None:
                unknown = set(dep.platforms) - set(mod.platforms)
                if unknown:
                    warnings.append(
                        f"模块 {mod.name} 的依赖 {dep.describe()} 限定了平台 "
                        f"{sorted(unknown)},但该模块本身不为这些平台构建"
                    )

    all_contracts = {c for m in repo.modules.values() for c in m.contracts}
    for t in repo.tests:
        for c in t.covers_contracts:
            if c not in all_contracts:
                warnings.append(f"测试 {t.name} 覆盖了没有任何模块声明的契约: {c}")
        for m in t.covers_modules:
            if m.name and m.name not in names:
                raise SpecError(f"测试 {t.name} 覆盖了不存在的模块: {m.name}")
            if m.pattern and not any(glob_to_regex(m.pattern).match(n) for n in names):
                warnings.append(f'测试 {t.name} 的通配符 "{m.pattern}" 没有匹配到任何模块')
    return warnings
