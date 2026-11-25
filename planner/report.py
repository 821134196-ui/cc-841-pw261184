"""输出:plan.json(给 CI 消费)+ report.html(给人看,可反点溯源)。

报告遵循 dataviz 规范:
  * 统计用裸 stat tile;预算用单一度量 meter(顺序蓝色单色相);
  * 状态色仅用于状态(好/警告/严重/危急),且永远 图标+文字 成对出现;
  * 文字只用墨色 token,不用系列色;表格数字 tabular-nums;
  * 暗色模式是独立选定的一套值(media query + 手动 toggle),不是自动反色。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

ESC = html.escape

KIND_LABEL = {"contract": "通用契约", "arch": "架构专用", "e2e": "端到端"}
SEVERITY_LABEL = {"critical": "危急", "high": "高", "medium": "中"}
SEVERITY_ICON = {"critical": "✖", "high": "▲", "medium": "●"}


def write_outputs(plan: dict, out_dir) -> tuple:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / "plan.json"
    html_path = out_dir / "report.html"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(plan), encoding="utf-8")
    return plan_path, html_path


# ---------------------------------------------------------------------------
# HTML 片段
# ---------------------------------------------------------------------------

def _trace_html(trace: dict) -> str:
    if not trace:
        return "<p class='muted'>无溯源信息</p>"
    steps = []
    for s in trace["steps"]:
        note = f"<span class='via'>{ESC(s['note'])}</span>" if s["note"] else ""
        edge = ""
        if s.get("edge"):
            e = s["edge"]
            cls = "tag tag-wild" if e["via"] == "wildcard" else "tag"
            kind = "通配符" if e["via"] == "wildcard" else "显式"
            edge = f" <span class='{cls}'>{kind}</span>"
            if e.get("platforms"):
                edge += f" <span class='tag tag-plat'>仅 {ESC(','.join(e['platforms']))}</span>"
        steps.append(f"<li><code>{ESC(s['module'])}</code> {note}{edge}</li>")
    cycle = ""
    if trace.get("cycle_note"):
        cycle = f"<p class='cycle-note'>↻ {ESC(trace['cycle_note'])}</p>"
    return f"<ol class='trace'>{''.join(steps)}</ol>{cycle}"


def _chip(text: str, cls: str = "") -> str:
    return f"<span class='chip {cls}'>{ESC(text)}</span>"


def _stat_tile(label: str, value, sub: str = "") -> str:
    sub_html = f"<div class='stat-sub'>{ESC(sub)}</div>" if sub else ""
    return (
        f"<div class='stat'><div class='stat-label'>{ESC(label)}</div>"
        f"<div class='stat-value'>{ESC(str(value))}</div>{sub_html}</div>"
    )


def _budget_meter(tests: dict) -> str:
    if tests.get("budget") is None:
        return ""
    budget, total = tests["budget"], tests["total_cost"]
    pct = min(100.0, total * 100.0 / budget) if budget else 100.0
    if tests["risks_over_budget"]:
        chip = "<span class='status status-critical'>✖ 预算不足,存在未覆盖项</span>"
    elif total > budget * 0.9:
        chip = "<span class='status status-warning'>⚠ 预算接近上限</span>"
    else:
        chip = "<span class='status status-good'>✓ 预算内</span>"
    return f"""
    <div class="meter-block">
      <div class="meter-head"><span>测试预算</span>{chip}</div>
      <div class="meter" role="img" aria-label="预算已用 {total} 分钟,共 {budget} 分钟">
        <div class="meter-fill" style="width:{pct:.1f}%"></div>
      </div>
      <div class="meter-fig">已用 {total} / {budget} 分钟 · 剩余 {tests['remaining']} 分钟</div>
    </div>"""


def _filters_row(prefix: str, selects: list) -> str:
    """每张表一行的过滤器:文本搜索 + 维度下拉。"""
    parts = [
        f"<input type='search' id='{prefix}-q' placeholder='过滤…' "
        f"oninput=\"filterTable('{prefix}')\" aria-label='文本过滤'>"
    ]
    for label, attr, options in selects:
        opts = "".join(f"<option value='{ESC(v)}'>{ESC(t)}</option>" for v, t in options)
        parts.append(
            f"<select id='{prefix}-{attr}' onchange=\"filterTable('{prefix}')\" "
            f"aria-label='{ESC(label)}'><option value=''>{ESC(label)}:全部</option>{opts}</select>"
        )
    return f"<div class='filters'>{''.join(parts)}</div>"


def render_html(plan: dict) -> str:
    tests = plan["tests"]
    risks = plan["risks"]
    invalidated = plan["invalidated"]
    platforms = sorted({i["platform"] for i in invalidated})

    # ---- stat tiles ----
    tiles = "".join([
        _stat_tile("变更模块", len(plan["changes"]["modules"])),
        _stat_tile("失效制品", len(invalidated)),
        _stat_tile("入选测试", len(tests["selected"]), f"共 {tests['total_cost']} 分钟"),
        _stat_tile("覆盖义务", f"{tests['obligations_covered']}/{tests['obligations_total']}"),
        _stat_tile("未覆盖风险", len(risks), "需要处理" if risks else "无"),
    ])

    # ---- 变更与图解析 ----
    changed_items = "".join(f"<li><code>{ESC(m)}</code></li>" for m in plan["changes"]["modules"])
    unmapped = ""
    if plan["changes"]["unmapped"]:
        items = "".join(f"<li><code>{ESC(u)}</code></li>" for u in plan["changes"]["unmapped"])
        unmapped = (
            f"<div class='callout warn'><span class='status status-warning'>⚠ 未映射的变更</span>"
            f"<p>以下变更不属于任何模块的 paths,未参与影响面计算:</p><ul>{items}</ul></div>"
        )
    wild = ""
    if plan["wildcard_edges"]:
        rows = "".join(
            f"<tr><td><code>{ESC(w['consumer'])}</code></td><td><code>{ESC(w['pattern'])}</code></td>"
            f"<td>{'、'.join(f'<code>{ESC(m)}</code>' for m in w['matched'])}</td>"
            f"<td>{ESC(','.join(w['platforms'])) if w['platforms'] else '全部'}</td></tr>"
            for w in plan["wildcard_edges"]
        )
        wild = (
            "<h3>通配符依赖解析</h3><p class='muted'>通配符每次运行重新展开,成员增删即时生效。</p>"
            f"<table><thead><tr><th>消费者</th><th>模式</th><th>本次匹配</th><th>平台限定</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    cyc = ""
    if plan["cycles"]:
        items = "".join(
            f"<li>组 #{c['id']}:{' ↔ '.join(f'<code>{ESC(m)}</code>' for m in c['members'])}"
            f"<span class='muted'> —— 任一成员变更,全组制品失效</span></li>"
            for c in plan["cycles"]
        )
        cyc = f"<h3>循环依赖</h3><ul class='cyc'>{items}</ul>"

    # ---- 失效制品表 ----
    inv_rows = []
    for i, inv in enumerate(invalidated):
        direct = _chip("● 直接变更", "chip-direct") if inv["direct"] else _chip("○ 传递影响")
        inv_rows.append(
            f"<tr data-platform='{ESC(inv['platform'])}' "
            f"data-text='{ESC(inv['artifact'].lower())}'>"
            f"<td><code>{ESC(inv['artifact'])}</code></td>"
            f"<td>{ESC(inv['platform'])}</td><td>{direct}</td>"
            f"<td><details><summary>为什么失效</summary>{_trace_html(inv['trace'])}</details></td></tr>"
        )
    inv_table = (
        _filters_row("inv", [("平台", "platform", [(p, p) for p in platforms])])
        + "<table id='inv-table'><thead><tr><th>制品</th><th>平台</th><th>影响方式</th>"
        "<th>失效原因(可展开)</th></tr></thead>"
        f"<tbody>{''.join(inv_rows)}</tbody></table>"
    )

    # ---- 必跑测试表(反点:每个测试为什么入选) ----
    test_rows = []
    for entry in tests["selected"]:
        t = entry["test"]
        obs = []
        for ob in entry["obligations"]:
            direct = "直接变更" if ob["direct"] else "传递影响"
            obs.append(
                f"<li><strong>{ESC(ob['describe'])}</strong>"
                f"<span class='muted'>({direct})</span>"
                f"<details><summary>溯源到变更</summary>{_trace_html(ob['trace'])}</details></li>"
            )
        plats = "、".join(t["platforms"]) if t["platforms"] else "任意平台"
        test_rows.append(
            f"<tr data-kind='{ESC(t['kind'])}' data-text='{ESC(t['name'].lower())}'>"
            f"<td><code>{ESC(t['name'])}</code></td>"
            f"<td>{_chip(KIND_LABEL[t['kind']], 'chip-' + t['kind'])}</td>"
            f"<td class='num'>{t['cost']}</td><td>{ESC(plats)}</td>"
            f"<td><details><summary>{len(entry['obligations'])} 项义务 · 为什么入选</summary>"
            f"<ul class='obs'>{''.join(obs)}</ul></details></td></tr>"
        )
    test_table = (
        _filters_row("tst", [("类型", "kind", [(k, v) for k, v in KIND_LABEL.items()])])
        + "<table id='tst-table'><thead><tr><th>测试</th><th>类型</th><th>成本(分钟)</th>"
        "<th>平台</th><th>入选理由(可反点)</th></tr></thead>"
        f"<tbody>{''.join(test_rows)}</tbody></table>"
    )

    # ---- 风险 ----
    if risks:
        risk_cards = []
        for r in risks:
            sev = r["severity"]
            chip = (
                f"<span class='status status-{sev}'>"
                f"{SEVERITY_ICON[sev]} {SEVERITY_LABEL[sev]} · {ESC(r['id'])}</span>"
            )
            nums = ""
            if r["kind"] == "budget":
                nums = (
                    f"<p class='risk-nums'>最便宜候选需 <strong>{r['needed_cost']}</strong> 分钟,"
                    f"剩余预算 <strong>{r['remaining_budget']}</strong> 分钟</p>"
                )
            risk_cards.append(
                f"<div class='risk risk-{sev}'>{chip}"
                f"<h4>{ESC(r['summary'])}</h4>"
                f"<p>{ESC(r['detail'])}</p>{nums}"
                f"<details><summary>影响如何传导到这里</summary>{_trace_html(r['trace'])}</details>"
                f"</div>"
            )
        risk_html = "".join(risk_cards)
    else:
        risk_html = "<p class='ok'><span class='status status-good'>✓ 全部覆盖</span> 预算内没有遗留未覆盖的义务。</p>"

    budget_chip = ""
    if tests.get("budget") is not None:
        budget_chip = (
            "<span class='status status-critical'>✖ 有未覆盖风险</span>"
            if risks else "<span class='status status-good'>✓ 预算内全覆盖</span>"
        )

    return _TEMPLATE.replace("/*__CSS__*/", _CSS) \
        .replace("__TITLE__", "变更影响面计划") \
        .replace("__GENERATED__", ESC(plan["generated_at"])) \
        .replace("__BUDGET_CHIP__", budget_chip) \
        .replace("__TILES__", tiles) \
        .replace("__METER__", _budget_meter(tests)) \
        .replace("__CHANGED__", changed_items) \
        .replace("__UNMAPPED__", unmapped) \
        .replace("__WILDCARD__", wild) \
        .replace("__CYCLES__", cyc) \
        .replace("__INV_TABLE__", inv_table) \
        .replace("__TEST_TABLE__", test_table) \
        .replace("__RISKS__", risk_html) \
        .replace("__GRAPH_DIR__", ESC(plan.get("graph_dir", "")))


_CSS = """
:root {
  color-scheme: light;
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --hairline: #e1e0d9; --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --good-ink: #006300;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --hairline: #2c2c2a; --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --good-ink: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-1: #1a1a19; --page: #0d0d0d;
  --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --hairline: #2c2c2a; --baseline: #383835;
  --border: rgba(255,255,255,0.10);
  --series-1: #3987e5;
  --good-ink: #0ca30c;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.55;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 24px 20px 64px; }
header.top { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px;
  border-bottom: 1px solid var(--hairline); padding-bottom: 16px; }
header.top h1 { font-size: 22px; margin: 0; flex: 1 1 auto; }
header.top .meta { color: var(--muted); font-size: 13px; }
button.theme { background: none; border: 1px solid var(--baseline); border-radius: 6px;
  color: var(--ink-2); padding: 4px 10px; cursor: pointer; font-size: 13px; }
h2 { font-size: 17px; margin: 36px 0 4px; }
h3 { font-size: 15px; margin: 20px 0 6px; }
.muted { color: var(--muted); }
section > p.desc { color: var(--ink-2); margin: 4px 0 12px; font-size: 14px; }

.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px; margin-top: 20px; }
.stat { background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 14px; }
.stat-label { color: var(--muted); font-size: 12.5px; }
.stat-value { font-size: 26px; margin-top: 2px; }
.stat-sub { color: var(--ink-2); font-size: 12.5px; }

.meter-block { background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px; margin-top: 12px; }
.meter-head { display: flex; justify-content: space-between; align-items: center;
  font-size: 14px; margin-bottom: 8px; }
.meter { height: 10px; background: var(--hairline); border-radius: 5px; overflow: hidden; }
.meter-fill { height: 100%; background: var(--series-1);
  border-radius: 0 4px 4px 0; }
.meter-fig { color: var(--ink-2); font-size: 13px; margin-top: 6px;
  font-variant-numeric: tabular-nums; }

.status { font-size: 13px; font-weight: 600; white-space: nowrap; }
.status-good { color: var(--good-ink); }
.status-warning { color: #0b0b0b; background: var(--warning);
  border-radius: 4px; padding: 1px 6px; }
.status-serious { color: var(--serious); }
.status-critical { color: var(--critical); }

.chip { display: inline-block; border: 1px solid var(--baseline); border-radius: 10px;
  padding: 0 8px; font-size: 12px; color: var(--ink-2); white-space: nowrap; }
.chip-direct { border-color: var(--ink-2); color: var(--ink); font-weight: 600; }
.tag { font-size: 11.5px; border: 1px solid var(--hairline); border-radius: 4px;
  padding: 0 5px; color: var(--muted); }
.tag-wild { border-style: dashed; color: var(--ink-2); }
.tag-plat { color: var(--ink-2); }

table { width: 100%; border-collapse: collapse; background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  font-size: 14px; margin-top: 4px; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--hairline);
  vertical-align: top; }
th { color: var(--muted); font-size: 12.5px; font-weight: 600; }
tbody tr:last-child td { border-bottom: none; }
td.num { font-variant-numeric: tabular-nums; text-align: right; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }

.filters { display: flex; gap: 8px; margin: 10px 0 6px; flex-wrap: wrap; }
.filters input, .filters select {
  background: var(--surface-1); color: var(--ink); border: 1px solid var(--baseline);
  border-radius: 6px; padding: 5px 10px; font-size: 13px; }
.filters input { flex: 1 1 180px; }

details summary { cursor: pointer; color: var(--ink-2); font-size: 13px; }
details[open] summary { margin-bottom: 6px; }
.trace { list-style: none; margin: 4px 0; padding: 0 0 0 2px; font-size: 13px; }
.trace li { padding: 3px 0 3px 18px; position: relative; }
.trace li::before { content: "→"; position: absolute; left: 0; color: var(--muted); }
.trace li:first-child::before { content: "●"; color: var(--critical); }
.cycle-note { font-size: 12.5px; color: var(--ink-2); border-left: 3px solid var(--warning);
  padding: 2px 8px; margin: 6px 0; }
ul.obs { margin: 4px 0; padding-left: 18px; }
ul.obs li { margin-bottom: 8px; }
ul.cyc li { margin: 4px 0; }

.callout { border: 1px solid var(--border); border-left: 3px solid var(--warning);
  background: var(--surface-1); border-radius: 6px; padding: 10px 14px; margin-top: 12px;
  font-size: 14px; }
.risk { border: 1px solid var(--border); background: var(--surface-1);
  border-radius: 8px; padding: 12px 16px; margin-top: 10px; font-size: 14px; }
.risk h4 { margin: 6px 0 4px; font-size: 15px; }
.risk p { margin: 4px 0; }
.risk-critical { border-left: 3px solid var(--critical); }
.risk-high { border-left: 3px solid var(--serious); }
.risk-medium { border-left: 3px solid var(--warning); }
.risk-nums { color: var(--ink-2); font-variant-numeric: tabular-nums; }
.ok { font-size: 14px; }
footer { margin-top: 40px; color: var(--muted); font-size: 12.5px;
  border-top: 1px solid var(--hairline); padding-top: 12px; }
"""

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>/*__CSS__*/</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>__TITLE__</h1>
    __BUDGET_CHIP__
    <button class="theme" onclick="toggleTheme()" aria-label="切换明暗主题">◐ 主题</button>
    <div class="meta">生成于 __GENERATED__ · 图目录 <code>__GRAPH_DIR__</code></div>
  </header>

  <div class="stats">__TILES__</div>
  __METER__

  <section>
    <h2>变更与依赖图解析</h2>
    <p class="desc">本次变更命中的模块,以及图中需要说明的三类结构:通配符依赖、循环依赖、平台限定边。</p>
    <h3>变更模块</h3>
    <ul>__CHANGED__</ul>
    __UNMAPPED__
    __WILDCARD__
    __CYCLES__
  </section>

  <section>
    <h2>需要失效的制品</h2>
    <p class="desc">按 (模块 × 平台) 计算;平台限定边只让对应平台的制品失效。每行可展开查看从变更源到该制品的传导链。</p>
    __INV_TABLE__
  </section>

  <section>
    <h2>必跑测试</h2>
    <p class="desc">通用契约测试只清偿显式声明的契约义务;架构/端到端测试清偿 (模块, 平台) 义务。点开「为什么入选」可反查到每条义务的变更溯源。</p>
    __TEST_TABLE__
  </section>

  <section>
    <h2>未覆盖风险</h2>
    <p class="desc">预算内无法安排、或根本没有候选测试的义务。这些是随本次发布上线的具体缺口。</p>
    __RISKS__
  </section>

  <footer>pointwise-delivery · 计划数据见同目录 plan.json · 通用测试仅覆盖显式声明的契约,不替代架构专用用例</footer>
</div>
<script>
function toggleTheme() {
  var r = document.documentElement;
  var cur = r.getAttribute('data-theme');
  var next = cur === 'dark' ? 'light' : 'dark';
  r.setAttribute('data-theme', next);
  try { localStorage.setItem('pd-theme', next); } catch (e) {}
}
try {
  var saved = localStorage.getItem('pd-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
} catch (e) {}
function filterTable(prefix) {
  var q = (document.getElementById(prefix + '-q').value || '').toLowerCase();
  var table = document.getElementById(prefix + '-table');
  var selects = table.previousElementSibling.querySelectorAll('select');
  var dims = [];
  selects.forEach(function (s) {
    if (s.value) dims.push({ attr: s.id.slice(prefix.length + 1), value: s.value });
  });
  table.querySelectorAll('tbody tr').forEach(function (tr) {
    var ok = !q || (tr.dataset.text || '').indexOf(q) !== -1;
    for (var i = 0; i < dims.length && ok; i++) {
      ok = tr.dataset[dims[i].attr] === dims[i].value;
    }
    tr.style.display = ok ? '' : 'none';
  });
}
</script>
</body>
</html>
"""
