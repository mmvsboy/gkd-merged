# GKD 四源合并订阅

合并 4 个在维护的 GKD 订阅源，自动去重、审核、开屏延时归零，每天定时跟进原源更新。

| 源 | id | 仓库 | 产物文件 |
|---|---|---|---|
| linarm | 667 | [Lin-arm/GKD_subscription](https://github.com/Lin-arm/GKD_subscription) | `dist/gkd.json5` |
| 梦念 | 1 | [MengNianxiaoyao/gkd-subscription](https://github.com/MengNianxiaoyao/gkd-subscription) | `dist/gkd.json5` |
| 甘霖 | 233 | [ganlinte/GKD-subscription](https://github.com/ganlinte/GKD-subscription) | `dist/ganlin_gkd.json5` |
| 奥怪 | 86 | [aoguai/subscription](https://github.com/aoguai/subscription) | `dist/aoguai_gkd.json5` |

## 文件说明

- `fetch_sources.py` — 下载 4 个源（主地址 raw.githubusercontent，失败自动换 jsDelivr 镜像），校验 id/version，任一失败整体退出
- `merge.py` — 合并 + 审核 + 输出 `dist/gkd.json5`，审核有错误则退出码 1（不产出坏文件到交付环节）
- `dist/gkd.json5` — 合并产物（纯 JSON，GKD 可直接订阅）
- `merge_report.json` — 每次合并的机器可读审核报告
- `.github/workflows/update.yml` — 每天自动跑「下载 → 合并 → 有变化才提交」

## 托管步骤（一次性）

1. 把本目录推到 GitHub（仓库名随意，建议 `gkd-merged`，**必须 Public** 才能用 raw 链接订阅）
2. 仓库页面 → `Settings` → `Actions` → `General` → Workflow permissions 选 **Read and write permissions** → Save
3. `Actions` 标签 → `update` 工作流 → 右侧 `Enable workflow`；可点 `Run workflow` 手动跑一次
4. 订阅链接（二选一，国内推荐 jsDelivr）：
   - `https://raw.githubusercontent.com/<你的用户名>/gkd-merged/main/dist/gkd.json5`
   - `https://cdn.jsdelivr.net/gh/<你的用户名>/gkd-merged@main/dist/gkd.json5`
5. GKD → 首页-订阅 → 右上角 `+` → 粘贴链接 → 确定

## 合并规则（都经过 GKD 官方文档 + 源码核对）

- **同名组合并**：不同源的同名组合并为一组（组级属性先下沉到规则，避免同按钮被多条规则重复点）
- **功能签名去重**：选择器+activity+动作完全相同的规则只留一条
- **key 重排 + 引用重映射**：preKeys / actionCdKey / actionMaximumKey 跟着改写到新 key；只保留「同组或 scopeKeys 组内」的引用（GKD 源码 bindGroupRules 的解析范围），源里本来就悬空的引用删除并记录
- **开屏广告延时归零**：所有开屏规则 `actionDelay`/`matchDelay` 显式设为 0（弹出来就点，不等倒计时）；`matchTime`（生效时间窗口）与 `actionCd`（点击后冷却）不动
- **默认开关**：开屏广告分类默认开，其余分类默认关（与 4 个源的实际默认状态一致：linarm/奥怪靠分类开关、梦念/甘霖靠组内开关，都是只有开屏在跑）。想要哪类，GKD 里把对应分类打开即整类生效
- **内置禁用（同名遮蔽）**：全局规则组在「已有同名前缀应用规则组」的 App 里自动停用，是 GKD 官方 `disableIfAppGroupMatch` 机制，防重复点击，不是错误

## 本地手动更新

```bash
pip install json5
python fetch_sources.py   # 下载 4 源
python merge.py           # 合并 + 审核，退出码 0 才算通过
```

## 审核口径（merge.py 审什么）

结构：id/name/version、分类、组名唯一、组 key/规则 key 唯一且为整数、选择器非空且括号引号配对、`~=` 值按 Kotlin 正则可编译、position 为合法对象、swipeArg 完整、action 在官方枚举内、versionCode/versionName 为合法匹配器对象；
引用：preKeys/actionCdKey/actionMaximumKey 必须指向本组或 scopeKeys 组内真实存在的 key；
语义：开屏规则延时必须为 0 且默认开，非开屏应用组默认关，组名归属已知分类。
