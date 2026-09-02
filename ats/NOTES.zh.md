# ATS 抓取工具层

> 中文技术笔记。英文总览见仓库根目录的 [README.md](../README.md)。
> 本文原是一个私有求职工作区里的内部文档，抽取成独立仓库时保留了原貌，个别对外部文件的引用已移除。

Ashby / Lever / Greenhouse 三家 ATS 的公开接口抓取。**没有浏览器自动化，没有 HTML 解析**
（只有 Lever 表单那一个例外），全部是 HTTP GET + `json.loads`。

复刻自 2026-08-26 那次扫描（757 个看板探测 / 450 个在线 / 951 条 AI 岗位），
产出 一份加拿大远程 Applied AI Engineer 岗位清单（原始产出，未包含在本仓库）。

---

## 为什么能直接打接口

这三家 ATS 都要给客户公司提供「嵌到自己官网 careers 页」的能力，所以看板数据必须
从浏览器端可读 —— 也就必然是公开的、无鉴权的 JSON 接口。抓 LinkedIn 要对抗反爬，
抓这三家只是在调它们本来就打算让你调的接口。

一次请求 = 一整个公司的全部在挂岗位。所以 1106 个看板全扫一遍是分钟级，不是小时级。

**「爬取」的实质是猜 slug。** 公司在 ATS 上的短名（`cohere` / `gitlab` / `benchsci`）
没有目录可查，只能拿一份候选表去批量探测：200 = 命中，404 = 不存在。
见 [`companies.txt`](companies.txt)。

想知道某公司的 slug，打开它的招聘页看 URL：

```
jobs.ashbyhq.com/<slug>/<uuid>          → Ashby
jobs.lever.co/<slug>/<uuid>             → Lever
job-boards.greenhouse.io/<slug>/jobs/<数字id>  → Greenhouse
```

---

## 三个接口

### Ashby

```
GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
```

三家里最慷慨的一个：**列表接口直接给全文 JD + 薪资区间 + `isRemote` 布尔位**，
抓完这一个请求基本不用再翻详情。代价是没有单岗详情接口 —— 要重取某条岗位，
得重拉整个看板再按 id 对回去。

```json
{"jobs": [{"id","title","location","secondaryLocations":[{"location"}],
           "department","team","employmentType","isRemote","publishedAt",
           "jobUrl","applyUrl","descriptionHtml","descriptionPlain","compensation":{}}]}
```

### Lever

```
GET https://api.lever.co/v0/postings/{slug}?mode=json          # 看板
GET https://api.lever.co/v0/postings/{slug}/{id}?mode=json     # 单岗
```

字段命名和另外两家完全不一样，接顶层数组不接对象。JD 正文拆在 `lists[]` 里
（Requirements / Responsibilities 各一段），要拼起来。

```json
[{"id","text","categories":{"location","allLocations":[],"commitment","department","team"},
  "workplaceType","createdAt","hostedUrl","applyUrl","descriptionPlain",
  "lists":[{"text","content"}],"additionalPlain"}]
```

### Greenhouse

```
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs                      # 看板（无正文）
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true         # 看板（含正文，慢）
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}                 # 单岗
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true  # 单岗 + 申请表单
```

```json
{"jobs":[{"id","title","location":{"name"},"absolute_url","updated_at",
          "metadata":[{"name","value"}],"requisition_id"}]}
```

### 字段映射

三家拉平成同一张表（[`ats.py`](ats.py) 里的三个 `scan_*` 函数）：

| 归一化字段 | Ashby | Lever | Greenhouse |
|---|---|---|---|
| `title` | `title` | **`text`** | `title` |
| `loc` | `location` + `secondaryLocations[]` | `categories.location` + `allLocations[]` | `location.name` + `metadata[]` |
| `posted` | `publishedAt`（ISO Z） | `createdAt`（**epoch 毫秒**） | `updated_at`（ISO 带偏移，**不可信**）|
| `emp` | `employmentType` | `categories.commitment` | — |
| `remote` | `isRemote`（布尔） | `workplaceType` | — |
| `url` | `jobUrl` / `applyUrl` | `hostedUrl` | `absolute_url` |
| 正文 | `descriptionHtml`（列表里就有） | `descriptionPlain` + `lists[]`（要拼） | 只在详情接口 |
| 薪资 | `compensation`（结构化） | — | 埋在正文里，要正则 |

---

## 三阶段流水线

```bash
# 1. 扫看板 → out/raw.json（标题命中）+ out/cand.json（粗筛后）
python3 ats/scan.py

# 2. 逐条取详情：真实发布日 / JD 信号 / 存活性 → out/enriched.json
python3 ats/enrich.py

# 3. 抓某个岗位的申请表单字段
python3 ats/forms.py <投递链接> --json applications/<包>/inputs/form-fields.json
```

常用变体：

```bash
python3 ats/scan.py --probe                      # 只探哪些 slug 活着
python3 ats/scan.py --slug cohere --ats ashby    # 单公司
python3 ats/scan.py --ats greenhouse --max-age 30
python3 ats/scan.py --now 2026-08-26             # 复现历史结果
python3 ats/enrich.py --workers 1 --delay 1.5    # 被限流时
```

**分工是刻意的：** 扫描阶段只用列表接口（快、能并发 40），能筛掉 95% 的噪音；
详情接口慢且会限流，只对活下来的候选打。

---

## 四个坑（都是踩出来的）

### 1. `updated_at` 不是发布日 ⚠️ 最坑的一个

Greenhouse 列表接口**只有 `updated_at`** —— 雇主上次动过这条记录的时间。改个错别字、
调一下部门、批量刷新，全都会更新它。用它判断「新岗」会把挂了半年的老岗当成昨天发的。

2026-08-28 全量跑的 28 条候选里，**15 条（54%）年龄被低估 3 倍以上**：

| 公司 | 岗位 | 列表 `updated_at` | 详情 `first_published` | 倍数 |
|---|---|---|---|---|
| Cresta | ML Engineering Intern | 24.9 天 | **716.7 天** | 29× |
| StackAdapt | Machine Learning Engineer | 9.8 天 | **365.6 天** | 37× |
| Pinterest | ML Engineer II（且已 403） | 30.8 天 | **248.7 天** | 8× |
| Dialpad | Software Engineer, ML Inference | 2.9 天 | **99.1 天** | 34× |
| Block | Applied Research Intern | 1.9 天 | **79.9 天** | 42× |
| Lyft | ML Engineer, Business & Ads | 1.8 天 | **53.0 天** | 29× |

**只看列表接口的话，Lyft / Block / Dialpad / Stripe 会排在最前面当「最新岗位」。**
实际最新的是 GitLab 那两条（7.7 / 7.8 天）—— 它们恰好是 `updated_at` 没说谎的。

真实发布日只在详情接口的 `first_published` 里，**必须逐条打详情**。
本工作区筛选口径里「用 `first_published` 而非 `updated_at`」那条规则就是从这儿来的。

Ashby 的 `publishedAt` 和 Lever 的 `createdAt` 没这个问题，列表里就是真的。

### 2. Python 的 SSL 证书 ⚠️ 最阴险的一个

macOS 自带 / python.org 装的 python3 常常没配 CA bundle，`urllib` 直接抛：

```
URLError <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]
          certificate verify failed: unable to get local issuer certificate>
```

同一个 URL，系统 `curl` 返回 200（用的是 keychain 根证书）。

**2026-08-26 第一轮扫描就栽在这上面：393 个看板全部"探测失败"，`live=0`。**
输出看起来完全正常 —— 只是"所有公司都没有看板"。一个请求都没真正发出去。

所以 [`ats.py`](ats.py) 的 `fetch()` 是 **curl 优先、urllib 兜底**，顺序跟直觉相反。

### 3. 详情接口会限流

看板列表接口开 40 线程没问题。详情接口并发高了会返回非 JSON（HTML 错误页），
`json.loads` 报错。`enrich.py` 默认 8 线程 + 退避重试；还被限就 `--workers 1 --delay 1.5`。

### 4. 404 也可能是合法 JSON

Greenhouse 对不存在的看板返回 **HTTP 404 + 一个结构完整的 JSON body**：

```json
{"status": 404, "error": "Job not found"}
```

`curl` 不加 `-f` 时 4xx 不算失败，`json.loads` 照样成功，`d.get("jobs", [])` 得到 `[]`
—— 于是「看板不存在」被静默误判成「看板在线但 0 个岗位」。命中率因此虚高一倍
（444 个 Greenhouse「在线」看板里有 328 个是幽灵）。

Ashby 的 404 返回纯文本 `Not Found`（`json.loads` 直接失败），Lever 返回非数组 JSON
（被类型检查挡住）—— **三家里只有 Greenhouse 会漏**。所以 `fetch()` 里显式判状态码，
不依赖 `json.loads` 失败来兜底。

值得注意的是这个 bug **没有影响任何岗位数据** —— 幽灵看板返回空数组，什么都没贡献。
它只污染了「覆盖了多少公司」这个统计量。这类 bug 最难发现：结果是对的，只有元数据错了。

### 5. HTML 实体与标签的解码顺序

Greenhouse 的 `content` 是**实体编码的 HTML**（`&lt;div class="content-intro"&gt;…`）。
`strip_html` 如果先去标签再解实体，第一步一个 `<` 都匹配不到，解完实体标签全漏进正文。

症状很隐蔽 —— 正文看着能读，只有信号里偶尔冒出半截标签：

```
年限=['3+ years in speech synthesis (<span data-highlig']    ← 修之前
年限=['3+ years in speech synthesis ( tts ) or applied ']    ← 修之后
```

正确顺序是**先解实体、再去标签、末尾再解一次**（处理标签内文本里的实体）。
Ashby 的 `descriptionHtml` 是真 HTML 不是实体编码，这个顺序对它也安全。

### 6. Ashby 表单字段名只能靠试

Ashby 关掉了 GraphQL introspection，报错里的字段建议也被隐藏（`[Suggestion hidden]`）。
实测 `descriptionHtml` ✅ / `descriptionPlain` ❌ / `description` ❌。
猜错任何一个，整个查询 `GRAPHQL_VALIDATION_FAILED` 整体失败，不是部分降级。

---

## 申请表单的可得性

| ATS | 可得性 | 怎么拿 |
|---|---|---|
| Greenhouse | ✅ 官方支持 | 详情接口加 `?questions=true`，返回完整 `questions[]` |
| Ashby | ✅ 非官方但公开 | 前端自用的 `jobs.ashbyhq.com/api/non-user-graphql`，无需鉴权。官方 `posting-api` 只有 JD，没有表单 |
| Lever | ⚠️ 不可得 | 公开 API 不暴露表单定义，只能退回去扒申请页 HTML 的 `input name`，可能不全 |

`--json` 落盘的是 ATS 原始数组，与 `applications/*/inputs/form-fields.json` 现有格式一致。

---

## 实测数据（2026-08-28 全量跑）

```
探测 1106 个看板  →  在线 294（命中率 27%）
    ashby        162 个看板 /  6,092 个在挂岗位
    greenhouse   116 个看板 / 11,096 个在挂岗位
    lever         16 个看板 /  1,264 个在挂岗位

18,452 个在挂岗位
  → 标题正则命中          947
  → 加拿大或泛北美地点      93
  → 距今 <= 45 天           61   ← 这一步用的是列表时间，不可信
  → 排除 senior/staff/lead  28
  → 富化后按真实发布日重排   15 条年龄被低估 3× 以上
```

耗时：全量扫描约 2 分钟，富化 28 条约 1 分钟。

（2026-08-26 那次报告的 450 个在线看板包含上面「坑 4」的幽灵看板，实际覆盖低于该数字。
岗位命中数 951 不受影响。）

---

## 这一层的边界

这里全部是**确定性代码**：同样的输入跑一万次结果一样，可测、可复现、可 diff。

不在这一层的（属于上层 agent 的判断）：

- **该扩哪些公司。** slug 只能猜，猜哪些取决于上一轮命中的是什么类型的公司
- **对自己的输出起疑。** `live=0` 到底是"没岗位"还是"网络挂了"，脚本分不清
- **口径本身错了。** 发现 `updated_at` 不可信 → 改用 `first_published`，这是改逻辑不是调参数
- **什么时候停。** 第三波扩表收益递减到什么程度算够

工具层的职责是**让这些判断有可靠的输入**。工具层不可靠的时候，上层 agent 会把
故障当成事实 —— 就像 `live=0` 那次，差点得出"加拿大没有 AI 岗"的结论。
