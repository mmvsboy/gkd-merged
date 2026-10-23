# -*- coding: utf-8 -*-
"""分析 4 个 GKD 订阅源的真实数据结构，为合并做准备（只读，不写文件）"""
import json, os, sys
from collections import Counter, defaultdict
import json5

BASE = os.path.dirname(os.path.abspath(__file__))
SOURCES = [
    ("linarm",   os.path.join(BASE, "sources", "linarm.json5")),
    ("mengnian", os.path.join(BASE, "sources", "mengnian.json5")),
    ("ganlinte", os.path.join(BASE, "sources", "ganlinte.json5")),
    ("aoguai",   os.path.join(BASE, "sources", "aoguai.json5")),
]

def norm_rules(rules_field):
    """rules 字段规范化成 dict 列表；返回 (list, kind) kind: 'str'|'list_str'|'list_obj'|'mixed'"""
    if isinstance(rules_field, str):
        return [{"matches": [rules_field]}], "str"
    if isinstance(rules_field, list):
        out, kinds = [], set()
        for r in rules_field:
            if isinstance(r, str):
                out.append({"matches": [r]}); kinds.add("str")
            elif isinstance(r, dict):
                d = dict(r)
                for f in ("matches", "anyMatches", "excludeMatches", "excludeAllMatches"):
                    if f in d and isinstance(d[f], str):
                        d[f] = [d[f]]
                out.append(d); kinds.add("obj")
        if not kinds:
            return out, "empty"
        return out, ("mixed" if len(kinds) > 1 else next(iter(kinds)))
    return [], "unknown"

report = {}
for name, path in SOURCES:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    sub = json5.loads(raw)
    apps = sub.get("apps") or []
    groups_total = rules_total = 0
    group_names = Counter()
    rule_kinds = Counter()
    group_level_props = Counter()
    group_enable = Counter()
    delay_vals = Counter()      # actionDelay 值分布（全部规则）
    mdelay_vals = Counter()     # matchDelay 值分布
    splash_delay = Counter()    # 开屏类组内 actionDelay 值分布
    splash_mdelay = Counter()
    prekeys = cdkey = maxkey = 0
    scopekeys = 0
    app_ids = set()
    dup_app = 0
    for app in apps:
        aid = app.get("id")
        if aid in app_ids: dup_app += 1
        app_ids.add(aid)
        for g in app.get("groups") or []:
            groups_total += 1
            group_names[g.get("name", "")] += 1
            group_enable[g.get("enable", "<absent>")] += 1
            for k in g.keys():
                if k not in ("key", "name", "rules", "desc"):
                    group_level_props[k] += 1
            if "scopeKeys" in g: scopekeys += 1
            rules, kind = norm_rules(g.get("rules"))
            rule_kinds[kind] += 1
            rules_total += len(rules)
            is_splash = str(g.get("name", "")).startswith("开屏广告")
            for r in rules:
                if "actionDelay" in r: delay_vals[r["actionDelay"]] += 1
                if "matchDelay" in r: mdelay_vals[r["matchDelay"]] += 1
                if is_splash:
                    splash_delay[r.get("actionDelay", "<absent>")] += 1
                    splash_mdelay[r.get("matchDelay", "<absent>")] += 1
                if r.get("preKeys"): prekeys += 1
                if "actionCdKey" in r: cdkey += 1
                if "actionMaximumKey" in r: maxkey += 1
    report[name] = dict(
        id=sub.get("id"), version=sub.get("version"), name=sub.get("name"),
        top_keys=sorted(sub.keys()),
        categories=[(c.get("key"), c.get("name"), c.get("enable", "<absent>")) for c in (sub.get("categories") or [])],
        global_groups=len(sub.get("globalGroups") or []),
        apps=len(apps), dup_app_ids=dup_app, groups=groups_total, rules=rules_total,
        rule_kinds=dict(rule_kinds), group_enable=dict(group_enable),
        group_level_props=dict(group_level_props),
        actionDelay=dict(delay_vals), matchDelay=dict(mdelay_vals),
        splash_actionDelay=dict(splash_delay), splash_matchDelay=dict(splash_mdelay),
        preKeys=prekeys, actionCdKey=cdkey, actionMaximumKey=maxkey, scopeKeys=scopekeys,
        file_bytes=len(raw),
    )
    print("=" * 70)
    print(f"[{name}] id={sub.get('id')} version={sub.get('version')} {sub.get('name')!r} ({len(raw)} bytes)")
    print(f"  apps={len(apps)} groups={groups_total} rules={rules_total} globalGroups={len(sub.get('globalGroups') or [])} dupAppIds={dup_app}")
    print(f"  categories: {report[name]['categories']}")
    print(f"  rules 字段形式分布: {dict(rule_kinds)}")
    print(f"  组级(非key/name/rules/desc)属性出现次数: {dict(group_level_props)}")
    print(f"  组级 enable 分布: {dict(group_enable)}  scopeKeys 组数: {scopekeys}")
    print(f"  actionDelay 值分布(全部): {dict(delay_vals)}")
    print(f"  matchDelay 值分布(全部): {dict(mdelay_vals)}")
    print(f"  开屏广告组内 actionDelay: {dict(splash_delay)}")
    print(f"  开屏广告组内 matchDelay: {dict(splash_mdelay)}")
    print(f"  preKeys 规则数={prekeys} actionCdKey={cdkey} actionMaximumKey={maxkey}")
    # 全局组结构
    for gg in (sub.get("globalGroups") or [])[:3]:
        rules_g, kind_g = norm_rules(gg.get("rules"))
        print(f"  globalGroup: key={gg.get('key')} name={gg.get('name')!r} enable={gg.get('enable','<absent>')} rules={len(rules_g)} kind={kind_g} props={sorted(k for k in gg.keys() if k not in ('key','name','rules'))}")

print("=" * 70)
print("全部组名清单（按出现次数排序，前 60）:")
allnames = Counter()
for name, path in SOURCES:
    with open(path, "r", encoding="utf-8") as f:
        sub = json5.loads(f.read())
    for app in sub.get("apps") or []:
        for g in app.get("groups") or []:
            allnames[g.get("name", "")] += 1
for n, c in allnames.most_common(60):
    print(f"  {c:5d}  {n}")
print(f"  ... 共 {len(allnames)} 个不同组名")

print("=" * 70)
print("不以已知类别名开头的组名（前 40）:")
known_prefixes = ("开屏广告", "青少年模式", "更新提示", "评价提示", "通知提示", "权限提示", "定位提示", "局部广告", "全屏广告", "分段广告", "功能类", "其他")
orphans = [(n, c) for n, c in allnames.most_common() if not n.startswith(known_prefixes)]
for n, c in orphans[:40]:
    print(f"  {c:5d}  {n!r}")
print(f"  ... 共 {len(orphans)} 个")
