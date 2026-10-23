# -*- coding: utf-8 -*-
"""
fetch_sources.py — 下载 4 个在维护的 GKD 订阅源到 sources/
每个源配置了 主地址(raw.githubusercontent) + 镜像(cdn.jsdelivr.net), 主地址失败自动换镜像;
任一源下载或校验失败即整体失败(退出码 1), 绝不产出半截合并。
地址均经实测验证(HTTP 206), 与各仓库 dist 目录结构一致:
  linarm   id=667  Lin-arm/GKD_subscription        dist/gkd.json5
  mengnian id=1    MengNianxiaoyao/gkd-subscription dist/gkd.json5
  ganlinte id=233  ganlinte/GKD-subscription        dist/ganlin_gkd.json5
  aoguai   id=86   aoguai/subscription (分支 custom) dist/aoguai_gkd.json5
"""
import os, sys, subprocess, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
SOURCES_DIR = os.path.join(BASE, "sources")

# (名字, 期望id, [主地址, 镜像1, 镜像2])
URLS = [
    ("linarm", 667, [
        "https://raw.githubusercontent.com/Lin-arm/GKD_subscription/main/dist/gkd.json5",
        "https://cdn.jsdelivr.net/gh/Lin-arm/GKD_subscription@main/dist/gkd.json5",
        "https://fastly.jsdelivr.net/gh/Lin-arm/GKD_subscription@main/dist/gkd.json5",
    ]),
    ("mengnian", 1, [
        "https://raw.githubusercontent.com/MengNianxiaoyao/gkd-subscription/main/dist/gkd.json5",
        "https://cdn.jsdelivr.net/gh/MengNianxiaoyao/gkd-subscription@main/dist/gkd.json5",
        "https://registry.npmmirror.com/gkd-subscription/latest/files/dist/gkd.json5",
    ]),
    ("ganlinte", 233, [
        "https://raw.githubusercontent.com/ganlinte/GKD-subscription/main/dist/ganlin_gkd.json5",
        "https://cdn.jsdelivr.net/gh/ganlinte/GKD-subscription@main/dist/ganlin_gkd.json5",
        "https://fastly.jsdelivr.net/gh/ganlinte/GKD-subscription@main/dist/ganlin_gkd.json5",
    ]),
    ("aoguai", 86, [
        "https://raw.githubusercontent.com/aoguai/subscription/custom/dist/aoguai_gkd.json5",
        "https://cdn.jsdelivr.net/gh/aoguai/subscription@custom/dist/aoguai_gkd.json5",
        "https://fastly.jsdelivr.net/gh/aoguai/subscription@custom/dist/aoguai_gkd.json5",
    ]),
]

def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "gkd-merge-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def main():
    os.makedirs(SOURCES_DIR, exist_ok=True)
    try:
        import json5
    except ImportError:
        print("需要 json5: pip install json5"); return 1
    failed = []
    for name, expect_id, urls in URLS:
        dest = os.path.join(SOURCES_DIR, f"{name}.json5")
        ok = False
        for url in urls:
            try:
                data = fetch(url)
                if len(data) < 1024:
                    print(f"[{name}] {url} 内容过小({len(data)}B), 跳过"); continue
                text = data.decode("utf-8")
                sub = json5.loads(text)
                if not isinstance(sub.get("id"), int) or not isinstance(sub.get("name"), str) \
                        or not isinstance(sub.get("version"), int):
                    print(f"[{name}] {url} 解析成功但 id/name/version 缺失, 跳过"); continue
                if sub["id"] != expect_id:
                    print(f"[{name}] {url} id={sub['id']} 与预期 {expect_id} 不符, 跳过"); continue
                # 与磁盘旧版相同则不重写(避免无谓 diff)
                if os.path.exists(dest):
                    old = open(dest, encoding="utf-8").read()
                    if old == text:
                        print(f"[{name}] 已是最新 v{sub['version']} ({len(data)}B, 无需更新)")
                        ok = True; break
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"[{name}] 下载成功 v{sub['version']} ({len(data)}B) <- {url}")
                ok = True; break
            except Exception as e:
                print(f"[{name}] {url} 失败: {e}")
        if not ok:
            failed.append(name)
    if failed:
        print(f"以下源全部地址失败: {failed} -> 放弃本次更新(不合并半截数据)")
        return 1
    print("4 个源全部就绪")
    return 0

if __name__ == "__main__":
    sys.exit(main())
