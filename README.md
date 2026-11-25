# pointwise-delivery — 变更影响面流水线计划器

多平台发布中,一个共享依赖变了,影响面远不止它所在的目录。本工具读取本地的
**依赖图**与**测试覆盖关系**,算出三件事:

1. **需要失效的制品** —— 按 `(模块 × 平台)` 精确计算,不是"变更目录"这种粗粒度;
2. **必跑测试** —— 每个测试都能反点出"为什么入选",一路溯源到变更文件;
3. **预算内的未覆盖风险** —— 测试成本超预算时,给出具体缺口,而不是静默跳过。

纯 Python 3 标准库,无第三方依赖。

## 快速开始

```bash
# 校验依赖图
python3 -m planner validate --graph examples/demo/graph

# 生成计划(预算 300 分钟)
python3 -m planner plan \
    --graph examples/demo/graph \
    --changes examples/demo/changes.txt \
    --budget 300 \
    --out examples/demo/out

# 跑测试
python3 -m unittest discover -s tests
```

输出:`plan.json`(给 CI 消费)+ `report.html`(给人看,自包含单文件,可反点溯源)。
`--fail-on-risk` 让存在未覆盖风险时以退出码 2 结束,供 CI 卡点。

## 输入

### `graph/modules.json` — 模块与依赖

```jsonc
{
  "modules": [
    {
      "name": "core/crypto",
      "paths": ["libs/crypto/**"],           // 变更文件 → 模块 的归属 glob
      "platforms": ["ios", "android", "web"],// 为哪些平台构建制品
      "contracts": ["crypto.sign"],          // 显式声明的对外契约
      "deps": [
        "core/base",                          // 显式依赖
        {"pattern": "feature/*"},             // 通配符依赖(每次运行重新展开)
        {"name": "platform/x", "platforms": ["ios"]}  // 平台限定依赖边
      ]
    }
  ]
}
```

### `graph/tests.json` — 测试与覆盖关系

```jsonc
{
  "tests": [
    { "name": "crypto-contract", "kind": "contract", "cost": 20,
      "covers_contracts": ["crypto.sign"] },                    // 通用契约测试
    { "name": "crypto-arch-ios", "kind": "arch", "cost": 45,
      "covers_modules": ["core/crypto"], "platforms": ["ios"] } // 架构专用测试
    // kind: contract | arch | e2e;cost 单位是分钟,与 --budget 一致
  ]
}
```

### `changes.txt` — 变更清单

每行一个**模块名**或**文件路径**(按模块 `paths` 的 glob 归属)。`#` 开头为注释。
无法归属的变更:默认警告并忽略;`--unmapped all` 时保守地全量失效。

## 计算规则

### 失效传播

以 `(模块, 平台)` 为状态做广度优先传播,三类结构都有明确语义:

| 结构 | 语义 |
|---|---|
| **循环依赖** | Tarjan 强连通分量识别;环上模块互为依赖,任一成员变更,全组制品失效,溯源链上标注"循环依赖组 #N" |
| **通配符依赖** | 每次运行重新展开成具体边(`*` 不跨目录、`**` 跨目录);报告列出每个 pattern 本次匹配到的模块,成员增删即时生效 |
| **平台专属构建** | 平台限定边只沿限定平台传播;模块只在自己声明的平台上产生失效制品——iOS 专属依赖的变更不会误伤 web 制品 |

### 覆盖义务与测试选择

每个失效的 `(模块, 平台)` 产生义务:

- **架构义务** `(模块, 平台)`:只能由 `arch`/`e2e` 测试清偿;
- **契约义务** `(模块, 契约)`:模块 `contracts` 里显式声明的每个契约一条,只能由 `contract` 测试清偿。

**硬规则:通用契约测试只能覆盖显式声明的契约,不能替代架构专用用例。**
一个模块只有契约测试而没有某平台的架构测试时,该平台的架构义务不会被
"通用测试跑过了"糊弄过去——它会成为一条明确的 `no-test` 风险。

选择算法是贪心集合覆盖:义务按优先级排序(直接变更的架构义务 > 传递的架构义务
> 直接变更的契约义务 > 传递的契约义务),候选测试按"新覆盖义务多、成本低"排序,
预算装不下就跳过并记账。

### 未覆盖风险

每条没清偿的义务生成一条风险,包含:

- **严重度**:直接变更的架构义务 = 危急;传递架构 / 直接契约 = 高;传递契约 = 中;
- **具体数字**:`budget` 类风险给出"最便宜候选需 X 分钟,剩余预算 Y 分钟";
- **具体缺口**:`no-test` 类风险说明该模块声明的契约已由通用测试覆盖、但契约之外
  的平台专属行为无验证;
- **完整溯源链**:从变更文件到该义务的每一步传播路径。

## 输出

### `plan.json`

```jsonc
{
  "changes":      { "modules": [...], "unmapped": [...] },
  "wildcard_edges": [ { "consumer", "pattern", "matched": [...], "platforms" } ],
  "cycles":       [ { "id", "members": [...], "note" } ],
  "invalidated":  [ { "artifact": "app/shell:ios", "direct": false, "trace": {...} } ],
  "tests":        { "selected": [ { "test": {...}, "obligations": [...] } ],
                    "total_cost", "budget", "remaining",
                    "obligations_covered", "obligations_total" },
  "risks":        [ { "id", "kind": "no-test|budget", "severity",
                      "obligation": {...}, "summary", "detail",
                      "needed_cost", "remaining_budget", "trace": {...} } ]
}
```

### `report.html`

自包含单文件(无外部资源),明暗双主题。每张表有过滤行;每个失效制品可展开
"为什么失效";**每个入选测试可反点"为什么入选"**——展开后是它清偿的义务清单,
每条义务还能继续展开到变更源的完整传导链。风险卡片带严重度(图标+文字,不
只靠颜色)与缺口数字。

## 演示数据

`examples/demo/` 是一个 10 模块的多平台仓库,故意包含:

- `feature/player ↔ feature/playlist` 循环依赖;
- `app/shell` 经 `feature/*` 通配符引用全部特性模块;
- `platform/keystore-{ios,android}` 平台专属模块 + 平台限定依赖边;
- `core/crypto` 声明了契约且有契约测试,但**没有 web 架构测试**——演示
  "通用测试不能替代架构专用用例"如何落成一条 `no-test` 风险;
- 预算 300 分钟恰好跑完高优先级测试,余下 2 条 `budget` 风险带具体缺口数字。

## 边界与说明

- 依赖图是**输入**而非本工具推导:通常由 BUILD 文件 / 包管理器清单生成
  `modules.json`;`contracts` 需要团队显式维护,这正是"通用测试覆盖边界"
  的来源——没声明的契约,通用测试的覆盖不被承认。
- 契约义务与平台无关(契约测试是平台通用的);架构义务与平台绑定。
  反向替代(架构测试清偿契约义务)同样不允许,保持职责清晰。
- 贪心集合覆盖不保证全局最优,但每一步选择都可解释、可复现(确定性排序),
  这对发布流水线比理论最优更重要。
