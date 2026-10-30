# -*- coding: utf-8 -*-
"""
merge.py — 合并 4 个在维护的 GKD 订阅源
流程: 解析 -> 规范化(含旧别名字段兼容) -> 同名组合并(组级属性下沉) -> 功能签名去重
      -> 全容器规则 key 重排 + 跨组引用重映射(仅同组/scopeKeys 组, 与 GKD bindGroupRules 一致)
      -> 开屏广告规则延时归零 -> 开关归一 -> 输出审核(零错误) -> dist/gkd.json5
依据: gkd.li 官方 API 文档(@gkd-kit/api 类型注释) + gkd-kit/gkd 源码
      (RawSubscription.kt 解析器 / ResolvedRule.kt bindGroupRules / RuleGroupPolicy.kt 开关策略)
"""
import json, os, re, sys, datetime
from collections import Counter, OrderedDict
import json5

BASE = os.path.dirname(os.path.abspath(__file__))
SOURCES = [
    ("linarm",   os.path.join(BASE, "sources", "linarm.json5")),
    ("mengnian", os.path.join(BASE, "sources", "mengnian.json5")),
    ("ganlinte", os.path.join(BASE, "sources", "ganlinte.json5")),
    ("aoguai",   os.path.join(BASE, "sources", "aoguai.json5")),
    # 本地补充规则：电视直播/電視直播 去广告（GKD 选择器规则，由 APK 实拆生成）
    ("tv",       os.path.join(BASE, "sources", "tv.json5")),
]
OUT_DIR = os.path.join(BASE, "dist")
OUT_FILE = os.path.join(OUT_DIR, "gkd.json5")

SUB_ID = 20261023
SUB_NAME = "四源合并订阅"
SUB_AUTHOR = "merged"

SPLASH_PREFIX = "开屏广告"
DELAY_FIELDS = ("actionDelay", "matchDelay")

# --- 开屏组防误点补丁 -------------------------------------------------------
# 甘霖源的全局开屏组第0条是官方示例式裸规则 [text*="跳过"][text.length<10],
# 无任何排除条件: 听书/播放类 App 打开即恢复播放页, 页面内的「跳过/跳过片头」
# 按钮在启动窗口期内会被误点。修复照抄奥怪源「兜底查询」的自带保护
# (text!*="视频"&&text!*="片头"&&text!*="片尾"), 并对全组追加 excludeMatches:
# 界面存在播放器拖动进度条(SeekBar)时整条规则不执行(官方 gkd_api_src.ts:
# excludeMatches 命中任一节点即停止匹配此规则), 开屏广告上不会有 SeekBar。
_BARE_SKIP = '[text*="跳过"][text.length<10][visibleToUser=true]'

def patch_splash_rule(r):
    """全局开屏组规则防误点加固, 返回是否修改。

    注意: 不要加 SeekBar 之类的 excludeMatches 排除。
    教训: 开屏广告「跳过」按钮自带倒计时进度条, 类名可能就是 SeekBar,
    加了会导致倒计时期间(约3秒)规则被压制、倒计时结束才出手 —— 用户实测
    「有跳过但3秒后才点」。四源作者均未加此类排除, 照抄原版, 只做文本排除。
    """
    changed = False
    ms = r.get("matches")
    if isinstance(ms, list):
        new_ms = []
        for m in ms:
            if m == _BARE_SKIP:
                new_ms.append('[text*="跳过"][text.length<10][text!*="片头"][text!*="片尾"][text!*="视频"][visibleToUser=true]')
                changed = True
            else:
                new_ms.append(m)
        if changed:
            r["matches"] = new_ms
    # 撤除此前误加的 SeekBar 排除(广告倒计时进度条会误命中, 拖慢出手)
    em = r.get("excludeMatches")
    if isinstance(em, list) and any("SeekBar" in str(x) for x in em):
        kept = [x for x in em if "SeekBar" not in str(x)]
        if kept:
            r["excludeMatches"] = kept
        else:
            r.pop("excludeMatches", None)
        changed = True
    return changed
# ---------------------------------------------------------------------------
SEL_FIELDS = ("matches", "anyMatches", "excludeMatches", "excludeAllMatches")
ACTION_ENUM = {"click", "clickNode", "clickCenter", "back", "longClick",
               "longClickNode", "longClickCenter", "swipe", "none"}
# 组级 -> 规则级下沉字段(官方 RawCommonProps/RawAppRuleProps 中组与规则共有的字段, 组级值即规则默认值;
# 依据 ResolvedRule.kt: RuleExecutionParameters(rule, group) 规则优先回退组)
FLATTEN_FIELDS = ("actionCd", "actionDelay", "fastQuery", "matchRoot", "matchDelay",
                  "matchTime", "actionMaximum", "resetMatch", "actionCdKey",
                  "actionMaximumKey", "order", "forcedTime", "priorityTime",
                  "priorityActionMaximum", "activityIds", "excludeActivityIds",
                  "versionNames", "excludeVersionNames", "versionCodes",
                  "excludeVersionCodes", "versionCode", "versionName")
LEGACY_ALIAS = (("delay", "actionDelay"), ("cd", "actionCd"))  # 解析器兼容旧名
# versionCode/versionName 官方类型是匹配器对象(IntegerMatcher/StringMatcher), 不是整数, 不在此列
INT_FIELDS = ("key", "actionCd", "actionDelay", "matchDelay", "matchTime", "actionMaximum",
              "actionCdKey", "actionMaximumKey", "order", "forcedTime", "priorityTime",
              "priorityActionMaximum")
POS_KEYS = {"left", "top", "right", "bottom", "x", "y"}

ERRORS, WARNS, INFOS = [], [], []
def err(m): ERRORS.append(m)
def warn(m): WARNS.append(m)
def info(m): INFOS.append(m)
stats = Counter()

# ---------------- 规范化 ----------------
def fix_legacy_alias(d, ctx):
    for legacy, canon in LEGACY_ALIAS:
        if legacy in d:
            if canon not in d:
                d[canon] = d.pop(legacy); stats["legacy_alias"] += 1
                info(f"{ctx} 旧别名字段 {legacy} -> {canon}")
            else:
                d.pop(legacy)  # 规范名优先(与 GKD 解析器一致)

def norm_rule(r, ctx):
    if not isinstance(r, dict):
        warn(f"{ctx} 规则不是对象({type(r).__name__}), 丢弃"); return None
    d = dict(r)
    fix_legacy_alias(d, ctx)
    for f in SEL_FIELDS:
        if f in d:
            v = d[f]
            if isinstance(v, str):
                d[f] = [v]
            elif isinstance(v, list):
                clean = [x for x in v if isinstance(x, str)]
                if len(clean) != len(v): warn(f"{ctx} {f} 含非字符串元素, 已过滤")
                if clean: d[f] = clean
                else: warn(f"{ctx} {f} 为空, 删除该字段"); d.pop(f)
            else:
                warn(f"{ctx} {f} 类型异常({type(v).__name__}), 删除该字段"); d.pop(f)
    if "preKeys" in d:
        v = d["preKeys"]
        if isinstance(v, int) and not isinstance(v, bool): d["preKeys"] = [v]
        elif isinstance(v, list):
            clean = [x for x in v if isinstance(x, int) and not isinstance(x, bool)]
            if len(clean) != len(v): warn(f"{ctx} preKeys 含非整数元素, 已过滤")
            d["preKeys"] = clean
        else:
            warn(f"{ctx} preKeys 类型异常({type(v).__name__}), 删除该字段"); d.pop("preKeys")
    return d

def norm_rules(rules_field, ctx):
    if rules_field is None: return []
    if isinstance(rules_field, str):
        d = norm_rule({"matches": rules_field}, ctx); return [d] if d else []
    if isinstance(rules_field, dict):
        d = norm_rule(rules_field, ctx); return [d] if d else []
    if isinstance(rules_field, list):
        out = []
        for i, r in enumerate(rules_field):
            if isinstance(r, str):
                d = norm_rule({"matches": r}, f"{ctx}.r[{i}]")
            elif isinstance(r, dict):
                d = norm_rule(r, f"{ctx}.r[{i}]")
            else:
                warn(f"{ctx}.r[{i}] 类型异常({type(r).__name__}), 丢弃"); continue
            if d: out.append(d)
        return out
    warn(f"{ctx} rules 字段类型异常({type(rules_field).__name__}), 按空处理")
    return []

def sel_tuple(r, f):
    v = r.get(f)
    if isinstance(v, list): return tuple(v)
    if isinstance(v, str): return (v,)
    return ()

def rule_func_sig(r):
    """功能签名: 相同签名=同一目标同一操作, 只保留一条"""
    return (sel_tuple(r, "matches"), sel_tuple(r, "anyMatches"),
            sel_tuple(r, "excludeMatches"), sel_tuple(r, "excludeAllMatches"),
            json.dumps(r.get("activityIds"), sort_keys=True, ensure_ascii=False),
            json.dumps(r.get("excludeActivityIds"), sort_keys=True, ensure_ascii=False),
            r.get("action"), json.dumps(r.get("position"), sort_keys=True, ensure_ascii=False))

def merge_app_lists(a, b, ctx):
    """全局组 apps(每 App 开关列表): 按 id 取并集, 冲突时排除(enable:false)优先"""
    if not (isinstance(a, list) and isinstance(b, list)):
        warn(f"{ctx} apps 结构异常, 保留首源值"); return a
    if not all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in a + b):
        warn(f"{ctx} apps 结构异常, 保留首源值"); return a
    by_id = OrderedDict()
    for e in a: by_id[e["id"]] = dict(e)
    conflicts = 0
    for e in b:
        eid = e["id"]
        if eid in by_id:
            conflicts += 1
            if e.get("enable") is False: by_id[eid]["enable"] = False
        else:
            by_id[eid] = dict(e)
    if conflicts:
        info(f"{ctx} {conflicts} 个 app 配置冲突, 已按并集合并(排除优先)")
    return list(by_id.values())

# ---------------- 容器(应用/全局)合并 ----------------
def add_group(container, label, g, where):
    name = (g.get("name") or "").strip()
    if not name:
        warn(f"[{label}] {where} 存在缺少 name 的组, 丢弃"); return
    fix_legacy_alias(g, f"[{label}] {where}/{name}")
    rules = norm_rules(g.get("rules"), f"[{label}] {where}/{name}")
    # 死规则: 无 matches 且无 anyMatches, 永远无法匹配
    alive = []
    for r in rules:
        if not r.get("matches") and not r.get("anyMatches"):
            warn(f"[{label}] {where}/{name} 存在无 matches/anyMatches 的死规则, 丢弃")
            stats["dead_rules"] += 1
        else:
            alive.append(r)
    rules = alive
    if not rules:
        info(f"[{label}] {where} 组 {name!r} 无有效规则, 丢弃"); stats["empty_groups_dropped"] += 1
        return
    for r in rules:
        for f in FLATTEN_FIELDS:
            if f in g and f not in r:
                r[f] = g[f]
        r["_src"] = label
        # GKD 的规则 key 按组作用域(RawSubscription.kt: rules.distinctNotNullBy{it.key} 在组内),
        # 同一源的不同组可复用 key, 故引用映射必须按 (源, 源组key, 旧key) 三维定位
        r["_gkey"] = g.get("key")
    group_only = {}
    for k, v in g.items():
        if k in ("key", "name", "rules") or k in FLATTEN_FIELDS: continue
        group_only[k] = v
    exist = container.get(name)
    if exist is None:
        container[name] = {"name": name, "rules": rules, "group_only": group_only,
                           "srcs": [label],
                           "old_group_keys": {label: g.get("key")},
                           "scope_keys_by_src": {label: group_only.get("scopeKeys") if isinstance(group_only.get("scopeKeys"), list) else []},
                           "enable_votes": [group_only.get("enable", True)]}
        stats["groups_new"] += 1
        return
    exist["srcs"].append(label)
    exist["old_group_keys"][label] = g.get("key")
    exist["scope_keys_by_src"][label] = group_only.get("scopeKeys") if isinstance(group_only.get("scopeKeys"), list) else []
    exist["enable_votes"].append(group_only.get("enable", True))
    for k, v in group_only.items():
        if k in ("enable", "scopeKeys"): continue
        if k not in exist["group_only"]:
            exist["group_only"][k] = v
        elif k == "apps":
            exist["group_only"][k] = merge_app_lists(exist["group_only"][k], v, f"{where}/{name}.apps")
        elif k == "ignoreGlobalGroupMatch":
            if exist["group_only"][k] != v:
                exist["group_only"][k] = bool(exist["group_only"][k]) or bool(v)
                warn(f"{where}/{name} ignoreGlobalGroupMatch 冲突, 取并集")
        elif k in ("snapshotUrls", "exampleUrls"):
            a = exist["group_only"][k]; b = v
            la = a if isinstance(a, list) else [a]; lb = b if isinstance(b, list) else [b]
            exist["group_only"][k] = la + [x for x in lb if x not in la]
        elif k == "desc":
            if not exist["group_only"][k] and v: exist["group_only"][k] = v
        else:
            if exist["group_only"][k] != v:
                warn(f"{where}/{name} 组级字段 {k} 多源冲突, 保留首源[{exist['srcs'][0]}]值")
    exist["rules"].extend(rules)
    stats["groups_merged"] += 1

def finalize_container(container, where, is_global):
    items = list(container.values())
    src_old_group_key = {}
    for idx, mg in enumerate(items):
        mg["_new_key"] = idx
        for src, oldk in mg["old_group_keys"].items():
            if isinstance(oldk, int) and not isinstance(oldk, bool):
                src_old_group_key[(src, oldk)] = idx
    # 1) 功能签名去重(在 key 重排之前, 避免被去重规则的 key 仍被他人引用);
    #    被去重规则的引用改指向保留的同功能规则(GKD 不去重功能, 保留近似语义)
    dup_alias = {}  # (源,源组key,旧key) -> 保留规则对象
    for mg in items:
        seen, out = {}, []
        for r in mg["rules"]:
            sig = rule_func_sig(r)
            if sig in seen:
                stats["dup_rules_dropped"] += 1
                dup_alias[(r["_src"], r.get("_gkey"), r.get("key"))] = seen[sig]
                continue
            seen[sig] = r; out.append(r)
        mg["rules"] = out
    # 2) 规则 key 全容器重排 + (源,源组key,旧key)->新key 及所属合并组 映射
    rule_key_map, rule_group_map, rule_new_key = {}, {}, {}
    nxt = 0
    for gi, mg in enumerate(items):
        for r in mg["rules"]:
            oldk = r.get("key")
            r["key"] = nxt; nxt += 1
            rule_new_key[id(r)] = r["key"]
            if isinstance(oldk, int) and not isinstance(oldk, bool):
                rule_key_map[(r["_src"], r.get("_gkey"), oldk)] = r["key"]
                rule_group_map[(r["_src"], r.get("_gkey"), oldk)] = gi
            else:
                if oldk is not None:
                    warn(f"[{r['_src']}] {where}/{mg['name']} 规则 key 类型异常({oldk!r}), 已重排")
                stats["keyless_rules"] += 1
    # 3) scopeKeys 重映射(先于规则引用, 引用合法性依赖它)
    for gi, mg in enumerate(items):
        mapped = []
        for src, keys in mg["scope_keys_by_src"].items():
            for k in keys:
                if not (isinstance(k, int) and not isinstance(k, bool)): continue
                nk = src_old_group_key.get((src, k))
                if nk is None:
                    warn(f"[{src}] {where}/{mg['name']} scopeKeys 引用的组 key={k} 在源内不存在, 已删除"); continue
                if nk != gi and nk not in mapped: mapped.append(nk)
        mg["_scope_new"] = set(mapped)
    # 4) 规则引用重映射: GKD bindGroupRules 只在 本组 + scopeKeys 组 内查找(源码证实),
    #    指向别处的引用在 GKD 里同样解析不到, 直接删除并记录
    for gi, mg in enumerate(items):
        allowed_groups = {gi} | mg["_scope_new"]
        for r in mg["rules"]:
            src, gkey = r["_src"], r.get("_gkey")
            def resolve(k):
                """返回 (新key 或 None, 失败原因)"""
                ident = (src, gkey, k)
                if ident in rule_key_map:
                    nk = rule_key_map[ident]
                    return (nk, None) if rule_group_map[ident] in allowed_groups else (None, "cross")
                if ident in dup_alias:
                    return rule_new_key[id(dup_alias[ident])], None  # 指向被去重规则 -> 改指保留规则
                return None, "missing"
            if isinstance(r.get("preKeys"), list):
                new = []
                for k in r["preKeys"]:
                    nk, why = resolve(k)
                    if nk is None:
                        if why == "missing":
                            warn(f"[{src}] {where}/{mg['name']} preKeys 引用 key={k} 在源内不存在(GKD 同样无法解析), 已删除")
                        else:
                            warn(f"[{src}] {where}/{mg['name']} preKeys 引用 key={k} 跨规则组且未经 scopeKeys 关联(GKD 无法解析), 已删除")
                        continue
                    new.append(nk)
                if new: r["preKeys"] = new
                else: r.pop("preKeys")
            for f in ("actionCdKey", "actionMaximumKey"):
                if f in r:
                    nk, why = resolve(r[f])
                    if nk is None:
                        warn(f"[{src}] {where}/{mg['name']} {f} 引用 key={r[f]} {'在源内不存在' if why == 'missing' else '跨规则组且未经 scopeKeys 关联(GKD 无法解析)'}, 已删除")
                        r.pop(f)
                    else: r[f] = nk
    # 5) 输出: 开屏延时归零 + 开关归一(开屏=开, 其余=关; 与四源实际默认状态一致)
    out_groups = []
    for gi, mg in enumerate(items):
        name = mg["name"]
        is_splash = name.startswith(SPLASH_PREFIX)
        if is_splash:
            for r in mg["rules"]:
                for f in DELAY_FIELDS:
                    if r.get(f, 0) != 0:
                        stats[f"splash_zero_{f}"] += 1; r[f] = 0
                    elif f not in r:
                        r[f] = 0
            stats["splash_rules"] += len(mg["rules"])
            if is_global:
                # 全局开屏组: 裸「跳过」规则防误点加固(播放页 SeekBar 排除等)
                for r in mg["rules"]:
                    if patch_splash_rule(r):
                        stats["splash_skip_patched"] = stats.get("splash_skip_patched", 0) + 1
        if is_global:
            enable = True if is_splash else any(mg["enable_votes"])
        else:
            enable = True if is_splash else False
        g = {"key": gi, "name": name}
        if mg["group_only"].get("desc"): g["desc"] = mg["group_only"]["desc"]
        g["enable"] = enable
        for k, v in mg["group_only"].items():
            if k in ("desc", "enable", "scopeKeys"): continue
            g[k] = v
        if mg["_scope_new"]:
            g["scopeKeys"] = sorted(mg["_scope_new"])
        for r in mg["rules"]:
            r.pop("_src", None); r.pop("_gkey", None)
        g["rules"] = mg["rules"]
        out_groups.append(g)
    return out_groups

# ---------------- 主流程 ----------------
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    apps = OrderedDict()
    globals_ = OrderedDict()
    cat_names = []

    for label, path in SOURCES:
        if not os.path.exists(path):
            err(f"源文件缺失: {path}"); continue
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            sub = json5.loads(raw)
        except Exception as e:
            err(f"源 [{label}] 解析失败: {e}"); continue
        if not isinstance(sub.get("id"), int) or not isinstance(sub.get("name"), str) or not isinstance(sub.get("version"), int):
            err(f"源 [{label}] id/name/version 缺失或类型错误"); continue
        info(f"源 [{label}] id={sub['id']} v{sub['version']} apps={len(sub.get('apps') or [])} 解析通过")
        for c in sub.get("categories") or []:
            n = c.get("name")
            if isinstance(n, str) and n and n not in cat_names: cat_names.append(n)
        for app in sub.get("apps") or []:
            aid = app.get("id")
            if not isinstance(aid, str) or not aid:
                warn(f"[{label}] 应用缺少合法 id, 丢弃"); continue
            entry = apps.get(aid)
            if entry is None:
                entry = {"name": app.get("name") if isinstance(app.get("name"), str) else "",
                         "fields": {k: v for k, v in app.items() if k not in ("id", "name", "groups")},
                         "container": OrderedDict()}
                apps[aid] = entry
            elif not entry["name"] and isinstance(app.get("name"), str):
                entry["name"] = app["name"]
            for g in app.get("groups") or []:
                add_group(entry["container"], label, g, aid)
            stats[f"src_{label}_rules"] += sum(len(norm_rules(g.get("rules"), "x")) for g in (app.get("groups") or []))
        for g in sub.get("globalGroups") or []:
            # 统一全局组命名: 去掉结尾的 "-全局" 后缀, 让不同源的同功能全局组合并为一组。
            # 否则会出现 "开屏广告" 与 "开屏广告-全局" 两套同时生效, 在无专属组的 App 里重复点击。
            if isinstance(g.get("name"), str):
                g = dict(g)
                g["name"] = re.sub(r"-全局$", "", g["name"])
            add_group(globals_, label, g, "global")
            stats[f"src_{label}_rules"] += len(norm_rules(g.get("rules"), "x"))

    ordered = [SPLASH_PREFIX] + [n for n in cat_names if n != SPLASH_PREFIX]
    categories = [{"key": i, "name": n, "enable": n == SPLASH_PREFIX} for i, n in enumerate(ordered)]

    out_apps = []
    for aid, entry in apps.items():
        groups = finalize_container(entry["container"], aid, is_global=False)
        if not groups:
            warn(f"应用 {aid} 合并后无有效规则组, 丢弃"); continue
        app_out = {"id": aid, "name": entry["name"] or aid}
        for k, v in entry["fields"].items(): app_out[k] = v
        app_out["groups"] = groups
        out_apps.append(app_out)
    out_globals = finalize_container(globals_, "global", is_global=True)

    # 版本号: 内容(除 version)不变则不涨
    today = int(datetime.date.today().strftime("%Y%m%d"))
    version = today
    if os.path.exists(OUT_FILE):
        try:
            old = json5.loads(open(OUT_FILE, encoding="utf-8").read())
            if isinstance(old.get("version"), int): version = old["version"]
        except Exception:
            pass
    subscription = {"id": SUB_ID, "name": SUB_NAME, "version": version,
                    "author": SUB_AUTHOR, "supportUri": "https://gkd.li/",
                    "categories": categories, "globalGroups": out_globals, "apps": out_apps}
    changed = True
    if os.path.exists(OUT_FILE):
        try:
            old = json5.loads(open(OUT_FILE, encoding="utf-8").read())
            old.pop("version", None); new_cmp = dict(subscription); new_cmp.pop("version")
            changed = json.dumps(old, sort_keys=True, ensure_ascii=False) != json.dumps(new_cmp, sort_keys=True, ensure_ascii=False)
        except Exception:
            changed = True
    if changed:
        version = max(version + 1, today)
    subscription["version"] = version
    text = json.dumps(subscription, ensure_ascii=False, separators=(",", ":"))
    try:
        json5.loads(text); json.loads(text)
    except Exception as e:
        err(f"输出文件自身解析失败: {e}")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(text)

    audit(subscription)

    total_groups = sum(len(a["groups"]) for a in out_apps) + len(out_globals)
    total_rules = sum(len(g["rules"]) for a in out_apps for g in a["groups"]) + sum(len(g["rules"]) for g in out_globals)
    print("=" * 74)
    print(f"输出: {OUT_FILE}  ({os.path.getsize(OUT_FILE)} 字节, version={subscription['version']}, 内容变化={changed})")
    print(f"应用 {len(out_apps)} | 组(应用+全局) {total_groups} | 规则 {total_rules}")
    print(f"同名组合并 {stats['groups_merged']} 次 | 重复规则去除 {stats['dup_rules_dropped']} 条 | 死规则丢弃 {stats['dead_rules']} 条 | 空组丢弃 {stats['empty_groups_dropped']} 个")
    print(f"无 key 规则自动补 key: {stats['keyless_rules']} 条 | 旧别名字段转换: {stats['legacy_alias']} 处")
    print(f"开屏规则: 共 {stats['splash_rules']} 条, 延时归零 actionDelay {stats['splash_zero_actionDelay']} 处 / matchDelay {stats['splash_zero_matchDelay']} 处")
    print(f"源规则总数: {sum(stats[f'src_{l}_rules'] for l, _ in SOURCES)}")
    print(f"分类({len(categories)}): " + ", ".join(f"{c['name']}={'开' if c['enable'] else '关'}" for c in categories))
    print("全局组:")
    for g in out_globals:
        print(f"  key={g['key']} {g['name']!r} enable={g['enable']} rules={len(g['rules'])}")
    print("-" * 74)
    print(f"审核: 错误 {len(ERRORS)} | 警告 {len(WARNS)} | 提示 {len(INFOS)}")
    for m in ERRORS[:30]: print("  [ERROR]", m)
    if len(ERRORS) > 30: print(f"  [ERROR] ...另 {len(ERRORS)-30} 项")
    for m in WARNS[:25]: print("  [WARN]", m)
    if len(WARNS) > 25: print(f"  [WARN] ...另 {len(WARNS)-25} 项")
    with open(os.path.join(BASE, "merge_report.json"), "w", encoding="utf-8") as f:
        json.dump({"errors": ERRORS, "warns": WARNS, "infos": INFOS, "stats": dict(stats),
                   "apps": len(out_apps), "groups": total_groups, "rules": total_rules,
                   "bytes": os.path.getsize(OUT_FILE), "version": subscription["version"]},
                  f, ensure_ascii=False, indent=1)
    print("=" * 74)
    if ERRORS:
        print("存在错误, 未通过交付审核"); return 1
    print("审核通过: 输出文件无错误")
    return 0

# ---------------- 审核(只审输出) ----------------
APPID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

def audit(sub):
    if not isinstance(sub.get("id"), int) or sub["id"] <= 0: err("订阅 id 非法")
    if not isinstance(sub.get("name"), str) or not sub["name"]: err("订阅 name 非法")
    if not isinstance(sub.get("version"), int): err("订阅 version 非法")
    cat_names = [c["name"] for c in sub["categories"]]
    if len(set(cat_names)) != len(cat_names): err("分类名重复")
    if SPLASH_PREFIX not in cat_names: err(f"缺少 {SPLASH_PREFIX} 分类")
    for c in sub["categories"]:
        if not isinstance(c.get("key"), int) or isinstance(c.get("key"), bool): err(f"分类 {c.get('name')} key 非法")
        if not isinstance(c.get("name"), str) or not c["name"]: err("分类 name 非法")
    cat_tuple = tuple(cat_names)
    seen_apps = set()
    for app in sub["apps"]:
        aid = app["id"]
        if aid in seen_apps: err(f"应用 id 重复: {aid}")
        seen_apps.add(aid)
        if not APPID_RE.match(aid): warn(f"应用 id 格式存疑: {aid}")
        if not app.get("name"): warn(f"应用 {aid} 缺少 name")
        # 第一遍: 组 key / 每组规则 key 收集(引用校验需要全组集合, GKD 组内查找顺序无关)
        group_keys, group_rule_keys = set(), {}
        for g in app["groups"]:
            if not isinstance(g.get("key"), int) or isinstance(g.get("key"), bool):
                err(f"{aid}/{g.get('name')} 组 key 非法"); continue
            if g["key"] in group_keys: err(f"{aid} 组 key 重复: {g['key']}")
            else: group_keys.add(g["key"])
            ks = set()
            for r in g.get("rules", []):
                k = r.get("key")
                if not isinstance(k, int) or isinstance(k, bool):
                    err(f"{aid}/{g['name']} 规则 key 非法: {k!r}")
                elif k in ks:
                    err(f"{aid}/{g['name']} 规则 key 重复: {k}")
                else:
                    ks.add(k)
            group_rule_keys[g["key"]] = ks
        # 第二遍: 逐组审核, 引用合法集合 = 本组规则 key + scopeKeys 组的规则 key
        for g in app["groups"]:
            allowed = set(group_rule_keys.get(g.get("key"), set()))
            for sk in g.get("scopeKeys", []):
                if sk in group_rule_keys: allowed |= group_rule_keys[sk]
            audit_group(g, f"{aid}/{g['name']}", allowed, cat_tuple, is_app=True)
        for g in app["groups"]:
            for k in g.get("scopeKeys", []):
                if k not in group_keys: err(f"{aid}/{g['name']} scopeKeys 引用不存在的组 key={k}")
    # 全局组
    gkeys, g_rule_keys = set(), {}
    for g in sub["globalGroups"]:
        if not isinstance(g.get("key"), int) or isinstance(g.get("key"), bool):
            err(f"global/{g.get('name')} 组 key 非法"); continue
        if g["key"] in gkeys: err(f"全局组 key 重复: {g['key']}")
        else: gkeys.add(g["key"])
        ks = set()
        for r in g.get("rules", []):
            k = r.get("key")
            if not isinstance(k, int) or isinstance(k, bool):
                err(f"global/{g['name']} 规则 key 非法: {k!r}")
            elif k in ks:
                err(f"global/{g['name']} 规则 key 重复: {k}")
            else:
                ks.add(k)
        g_rule_keys[g["key"]] = ks
    for g in sub["globalGroups"]:
        allowed = set(g_rule_keys.get(g.get("key"), set()))
        for sk in g.get("scopeKeys", []):
            if sk in g_rule_keys: allowed |= g_rule_keys[sk]
        audit_group(g, f"global/{g['name']}", allowed, cat_tuple, is_app=False)
        for k in g.get("scopeKeys", []):
            if k not in gkeys: err(f"global/{g['name']} scopeKeys 引用不存在的组 key={k}")
    # 同名遮蔽报告(官方 disableIfAppGroupMatch 机制): 全局组在有同前缀应用组的 App 里自动停用
    shadow_total = 0
    for g in sub["globalGroups"]:
        prefix = g.get("disableIfAppGroupMatch")
        if prefix is None: continue
        p = prefix if prefix != "" else g["name"]
        hit = [a["id"] for a in sub["apps"]
               if any((gg["name"].startswith(p)) and gg.get("ignoreGlobalGroupMatch") is not True
                      for gg in a["groups"])]
        if hit:
            shadow_total += len(hit)
            info(f"全局组 {g['name']!r} 按 disableIfAppGroupMatch={prefix!r} 在 {len(hit)} 个 App 内自动停用(防重复点击): {', '.join(hit[:8])}{'...' if len(hit) > 8 else ''}")
    if shadow_total:
        info(f"同名遮蔽合计 {shadow_total} 处(全局组让位应用专属组, GKD 官方机制, 非错误)")

def audit_group(g, ctx, allowed_keys, cat_tuple, is_app):
    if not isinstance(g.get("name"), str) or not g["name"]: err(f"{ctx} 组 name 非法")
    if "enable" in g and not isinstance(g["enable"], bool): err(f"{ctx} enable 类型错误")
    if "resetMatch" in g and g["resetMatch"] not in ("activity", "match", "app"):
        warn(f"{ctx} resetMatch 值异常: {g['resetMatch']!r}")
    for f in INT_FIELDS:
        if f in g and (not isinstance(g[f], int) or isinstance(g[f], bool)):
            err(f"{ctx} 组级 {f} 必须是整数: {g[f]!r}")
    if "scopeKeys" in g:
        if not isinstance(g["scopeKeys"], list) or any(not isinstance(k, int) or isinstance(k, bool) for k in g["scopeKeys"]):
            err(f"{ctx} scopeKeys 必须是整数数组")
    rules = g.get("rules")
    if not isinstance(rules, list) or not rules:
        err(f"{ctx} rules 为空或类型错误"); return
    for r in rules:
        if not isinstance(r.get("key"), int) or isinstance(r.get("key"), bool):
            err(f"{ctx} 规则 key 非法: {r.get('key')!r}")
        has_sel = False
        for f in SEL_FIELDS:
            if f in r:
                has_sel = True
                if not isinstance(r[f], list) or any(not isinstance(x, str) for x in r[f]):
                    err(f"{ctx} r{r.get('key')} {f} 必须是字符串数组")
                for s in r[f]:
                    audit_selector(s, f"{ctx} r{r.get('key')}.{f}")
        if not has_sel: err(f"{ctx} r{r.get('key')} 无任何 matches/anyMatches")
        if "action" in r and r["action"] not in ACTION_ENUM:
            err(f"{ctx} r{r.get('key')} action 非法: {r['action']!r}")
        audit_position(r.get("position"), f"{ctx} r{r.get('key')}")
        if "swipeArg" in r:
            sa = r["swipeArg"]
            if not isinstance(sa, dict): err(f"{ctx} r{r.get('key')} swipeArg 必须是对象")
            else:
                audit_position(sa.get("start"), f"{ctx} r{r.get('key')}.swipeArg.start", required=True)
                audit_position(sa.get("end"), f"{ctx} r{r.get('key')}.swipeArg.end")
                if not isinstance(sa.get("duration"), int) or isinstance(sa.get("duration"), bool):
                    err(f"{ctx} r{r.get('key')} swipeArg.duration 必须是整数")
        for f in INT_FIELDS:
            if f in r and (not isinstance(r[f], int) or isinstance(r[f], bool)):
                err(f"{ctx} r{r.get('key')} {f} 必须是整数: {r[f]!r}")
        for f, kind in (("versionCode", "int"), ("versionName", "str")):
            if f in r: audit_matcher(r[f], f"{ctx} r{r.get('key')}.{f}", kind)
        if isinstance(r.get("preKeys"), list):
            for k in r["preKeys"]:
                if k not in allowed_keys: err(f"{ctx} r{r.get('key')} preKeys 悬空引用 key={k}")
        for f in ("actionCdKey", "actionMaximumKey"):
            if f in r and r[f] not in allowed_keys:
                err(f"{ctx} r{r.get('key')} {f} 悬空引用 key={r[f]}")
        if g["name"].startswith(SPLASH_PREFIX):
            for f in DELAY_FIELDS:
                if r.get(f, 0) != 0: err(f"{ctx} r{r.get('key')} {f}={r[f]} 未归零")
    for f in ("actionCdKey", "actionMaximumKey"):
        if f in g and g[f] not in allowed_keys:
            err(f"{ctx} 组级 {f} 悬空引用 key={g[f]}")
    if g["name"].startswith(SPLASH_PREFIX):
        if g.get("enable") is not True: err(f"{ctx} 开屏组未启用")
        for f in DELAY_FIELDS:
            if f in g and g[f] != 0: err(f"{ctx} 开屏组组级 {f} 未归零")
    elif is_app:
        if g.get("enable") is not False: err(f"{ctx} 非开屏应用组 enable 必须为 false")
        if cat_tuple and not g["name"].startswith(cat_tuple):
            warn(f"{ctx} 组名不匹配任何分类(将不被分类开关管理)")

def audit_matcher(v, ctx, kind):
    """官方匹配器对象校验: IntegerMatcher{minimum,maximum,include,exclude} / StringMatcher{pattern,include,exclude}"""
    if not isinstance(v, dict):
        err(f"{ctx} 必须是匹配器对象: {v!r}"); return
    if kind == "int":
        keys = ("minimum", "maximum", "include", "exclude")
        bad = [k for k in v if k not in keys]
        if bad: err(f"{ctx} 含未知字段: {bad}")
        for k in ("minimum", "maximum"):
            if k in v and (not isinstance(v[k], int) or isinstance(v[k], bool)):
                err(f"{ctx}.{k} 必须是整数: {v[k]!r}")
        for k in ("include", "exclude"):
            if k in v and (not isinstance(v[k], list) or any(not isinstance(x, int) or isinstance(x, bool) for x in v[k])):
                err(f"{ctx}.{k} 必须是整数数组")
    else:
        keys = ("pattern", "include", "exclude")
        bad = [k for k in v if k not in keys]
        if bad: err(f"{ctx} 含未知字段: {bad}")
        if "pattern" in v and not isinstance(v["pattern"], str):
            err(f"{ctx}.pattern 必须是字符串")
        for k in ("include", "exclude"):
            if k in v and (not isinstance(v[k], list) or any(not isinstance(x, str) for x in v[k])):
                err(f"{ctx}.{k} 必须是字符串数组")

def audit_position(p, ctx, required=False):
    if p is None:
        if required: err(f"{ctx} position 缺失")
        return
    if not isinstance(p, dict):
        err(f"{ctx} position 必须是对象"); return
    bad = [k for k in p if k not in POS_KEYS]
    if bad: err(f"{ctx} position 含未知字段: {bad}")
    for k, v in p.items():
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            err(f"{ctx} position.{k} 必须是字符串或数字: {v!r}")
    if not any(k in p for k in ("left", "right", "x")):
        warn(f"{ctx} position 缺少水平方向字段(left/right/x)")
    if not any(k in p for k in ("top", "bottom", "y")):
        warn(f"{ctx} position 缺少垂直方向字段(top/bottom/y)")

def audit_selector(s, ctx):
    if not isinstance(s, str) or not s.strip():
        err(f"{ctx} 选择器为空"); return
    if any(c in s for c in "\n\t\r"):
        err(f"{ctx} 选择器含换行/制表符"); return
    stripped = re.sub(r'"[^"]*"', '""', s)
    stripped = re.sub(r"'[^']*'", "''", stripped)
    stripped = re.sub(r'`[^`]*`', '``', stripped)
    for a, b in (("[", "]"), ("(", ")"), ("{", "}")):
        if stripped.count(a) != stripped.count(b):
            err(f"{ctx} 选择器 {a}{b} 不配对: {s!r}"); return
    if s.count('"') % 2 != 0 or s.count("'") % 2 != 0:
        err(f"{ctx} 选择器引号不配对: {s!r}"); return
    # 仅 ~=/!~= 的值为 Kotlin 正则(官方文档), 需要校验;
    # 先做 GKD 选择器字符串层转义(\\ -> \\)再用 Python re 试编译
    for m in re.finditer(r'\[\s*[\w^$*!]+\s*!?~\s*=\s*"([^"]*)"\s*\]', s):
        val = m.group(1).replace('\\\\', '\\')
        try: re.compile(val)
        except re.error: warn(f"{ctx} ~= 正则可能无效: {m.group(1)!r} in {s!r}")
    for m in re.finditer(r"\[\s*[\w^$*!]+\s*!?~\s*=\s*'([^']*)'\s*\]", s):
        val = m.group(1).replace('\\\\', '\\')
        try: re.compile(val)
        except re.error: warn(f"{ctx} ~= 正则可能无效: {m.group(1)!r} in {s!r}")

if __name__ == "__main__":
    sys.exit(main())
