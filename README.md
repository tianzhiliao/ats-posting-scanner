# ATS Posting Scanner

Scans public Ashby, Lever, and Greenhouse job boards through their **official public APIs**, normalizes three incompatible schemas into one table, and verifies when each posting was *actually* first published.

No browser automation. No HTML parsing (one exception, noted below). No scraping. The scanner layer is HTTP GET plus `json.loads`, on the Python standard library — **nothing to install**.

## Why these APIs are public

All three vendors let customer companies embed their job board into their own careers page. That means the board data has to be readable from the browser, which means it has to be a public, unauthenticated JSON endpoint. Fighting a bot wall to scrape LinkedIn is a different activity from calling an endpoint that exists to be called.

One request returns every open posting at one company, so sweeping a thousand boards takes minutes, not hours.

```
GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
GET https://api.lever.co/v0/postings/{slug}?mode=json
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
```

**Discovery is slug guessing.** There is no directory of which companies use which ATS, so [`ats/companies.txt`](ats/companies.txt) is a candidate list probed in bulk: 200 means it exists, 404 means it does not. Measured hit rate on 2026-08-28: **27%** (1,106 probes, 294 live boards).

## Schema normalization

The three vendors agree on almost nothing. [`ats/ats.py`](ats/ats.py) flattens them into one row shape:

| Normalized | Ashby | Lever | Greenhouse |
|---|---|---|---|
| `title` | `title` | **`text`** | `title` |
| `loc` | `location` + `secondaryLocations[]` | `categories.location` + `allLocations[]` | `location.name` + `metadata[]` |
| `posted` | `publishedAt` (ISO Z) | `createdAt` (**epoch millis**) | `updated_at` (**not trustworthy — see below**) |
| `remote` | `isRemote` (boolean) | `workplaceType` | — |
| `url` | `jobUrl` / `applyUrl` | `hostedUrl` | `absolute_url` |
| body | `descriptionHtml`, in the list response | `descriptionPlain` + `lists[]`, must be joined | detail endpoint only |
| salary | `compensation`, structured | — | buried in prose, needs regex |

## The thing this tool exists for: `updated_at` lies

Greenhouse's list endpoint exposes only `updated_at` — the last time an employer *touched* the record. Fixing a typo, moving a department, or a bulk refresh all bump it. Use it to rank "newest postings" and you will put six-month-old listings at the top.

On a full run (2026-08-28), of 28 surviving candidates **15 (54%) had their age understated by 3× or more**:

| Company | Role | List `updated_at` | Detail `first_published` | Off by |
|---|---|---|---|---|
| Cresta | ML Engineering Intern | 24.9 days | **716.7 days** | 29× |
| StackAdapt | Machine Learning Engineer | 9.8 days | **365.6 days** | 37× |
| Pinterest | ML Engineer II (also already 403) | 30.8 days | **248.7 days** | 8× |
| Dialpad | Software Engineer, ML Inference | 2.9 days | **99.1 days** | 34× |
| Block | Applied Research Intern | 1.9 days | **79.9 days** | 42× |
| Lyft | ML Engineer, Business & Ads | 1.8 days | **53.0 days** | 29× |

Ranking on the list endpoint alone puts Lyft, Block, Dialpad and Stripe first as the "freshest" roles. The genuinely freshest were two GitLab postings at 7.7 and 7.8 days — they just happened to be the ones where `updated_at` was not lying.

The real date lives only in the detail endpoint's `first_published`, so **every surviving candidate gets a detail fetch**. Ashby's `publishedAt` and Lever's `createdAt` do not have this problem; they are honest in the list response.

## Three more failure modes worth knowing

**SSL verification, the quiet one.** macOS system Python and python.org builds often ship without a configured CA bundle, so `urllib` raises `CERTIFICATE_VERIFY_FAILED` while system `curl` returns 200 for the same URL. The first run of this scanner reported 393 boards as "probe failed" and `live=0` — output that looked entirely normal, describing a world where no company has a job board. Not one request had actually left the machine. `fetch()` in [`ats/ats.py`](ats/ats.py) therefore tries **curl first and falls back to urllib**, which is the opposite of the intuitive order.

**A 404 that is also valid JSON.** Greenhouse answers a nonexistent board with HTTP 404 *and* a well-formed body:

```json
{"status": 404, "error": "Job not found"}
```

`curl` without `-f` does not treat 4xx as failure, `json.loads` succeeds, and `d.get("jobs", [])` yields `[]` — so "this board does not exist" is silently recorded as "board is live with zero openings." That inflated the hit rate roughly twofold: of 444 Greenhouse boards counted as live, **328 were ghosts**. Ashby returns plain-text `Not Found` and Lever returns a non-array, so both fail loudly; only Greenhouse slips through. `fetch()` now checks the status code explicitly rather than relying on a parse failure.

What makes this class of bug hard: **it corrupted no job data at all.** Ghost boards return empty arrays and contribute nothing. Only the coverage statistic was wrong. The results were right and the metadata was wrong, which is the hardest kind to notice.

**Decode order for HTML entities.** Greenhouse `content` is entity-encoded HTML (`&lt;div class="content-intro"&gt;…`). Strip tags before decoding entities and the tag pass matches nothing, so tags leak into the body afterward. The symptom is subtle — prose that reads fine, with the occasional half-tag surfacing in an extracted field:

```
years=['3+ years in speech synthesis (<span data-highlig']
```

## Pipeline

```bash
# 1. Sweep boards -> out/raw.json (title matches) + out/cand.json (after coarse filter)
python3 ats/scan.py

# 2. Per-posting detail: true publish date, JD signals, liveness -> out/enriched.json
python3 ats/enrich.py

# 3. Pull the application form fields for one posting
python3 ats/forms.py <application-url> --json form-fields.json
```

Useful variants:

```bash
python3 ats/scan.py --probe                      # only test which slugs are alive
python3 ats/scan.py --slug cohere --ats ashby    # single company
python3 ats/scan.py --ats greenhouse --max-age 30
python3 ats/scan.py --now 2026-08-26             # reproduce a historical run
python3 ats/enrich.py --workers 1 --delay 1.5    # when rate limited
```

**The split is deliberate.** The scan stage touches only list endpoints — fast, safe at 40 threads, and enough to drop ~95% of the noise. Detail endpoints are slow and rate-limit, so they are reserved for candidates that already survived filtering. `enrich.py` defaults to 8 threads with backoff.

## Optional agent layer

[`agent/`](agent/) wraps the scanner in 8 tools and lets Claude drive them. **The scraping code is unchanged** — what changes is who decides the next move: which slugs to expand, whether `live=0` means "no jobs" or "the network broke", when a filtering criterion is itself wrong, and when to stop.

```bash
export ANTHROPIC_API_KEY=...
python3 agent/find_jobs.py "find remote mid-level Applied AI Engineer roles in Canada"
python3 agent/find_jobs.py --dry-run    # print the system prompt and tool list, spend nothing
```

The design constraint is response size, not capability: a full sweep is ~950 title matches, and returning those verbatim would bury the model in detail. Full results go to disk; **only aggregates, summaries, and short ids go into context.**

Requires `anthropic` (see [`requirements.txt`](requirements.txt)). The scanner layer does not.

## Responsible use

Official public posting APIs only, with capped concurrency, timeouts, and backoff. Read the vendors' terms before pointing this at anything, and keep request rates civil.

## Deeper notes

Longer engineering write-ups, in Chinese: [`ats/NOTES.zh.md`](ats/NOTES.zh.md) and [`agent/NOTES.zh.md`](agent/NOTES.zh.md).

## License

MIT

---

## 中文简介

用 Ashby / Lever / Greenhouse 三家 **官方公开接口** 扫描招聘看板，把三套互不兼容的字段归一成一张表，并核验每条岗位**真实的首次发布日**。

没有浏览器自动化，没有 HTML 解析（仅 Lever 表单一处例外），不是爬虫。扫描层是 HTTP GET + `json.loads`，纯标准库，**无需安装任何依赖**。

核心价值在一件事：**Greenhouse 列表接口的 `updated_at` 会撒谎**——它是雇主上次改动记录的时间，改个错别字就会刷新。2026-08-28 全量跑的 28 条候选里，15 条（54%）年龄被低估 3 倍以上，最夸张的 Cresta 是 24.9 天 vs 真实 716.7 天。真实发布日只在详情接口的 `first_published` 里。

另外三个坑同样是踩出来的：Python 的 SSL 证书问题让首轮 393 个看板**静默全灭**（`live=0`，输出看着完全正常）；Greenhouse 的 404 会返回结构完整的 JSON，导致 444 个「在线」看板里有 328 个是幽灵——**这个 bug 没污染任何岗位数据，只错了统计量，因此最难发现**；以及 HTML 实体与标签的解码顺序。

完整技术笔记见 [`ats/NOTES.zh.md`](ats/NOTES.zh.md) 与 [`agent/NOTES.zh.md`](agent/NOTES.zh.md)。
