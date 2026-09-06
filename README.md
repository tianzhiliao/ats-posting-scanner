# ATS Posting Scanner

Scans public Ashby, Lever, and Greenhouse job boards through their official APIs, normalizes their schemas, and inspects posting dates and job details before filtering candidates.

The scanner uses HTTP requests and Python standard-library parsing. It does not require browser automation or third-party Python packages. The optional agent layer has a separate dependency.

## Why these APIs are public

The three vendors provide public endpoints for published job boards. The scanner reads those endpoints directly:

A board response includes multiple postings, which keeps the first scan separate from the more expensive detail requests.

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
| `posted` | `publishedAt` (last publication, ISO Z) | `createdAt` (epoch millis) | `updated_at` (last update, not first publication) |
| `remote` | `isRemote` (boolean) | `workplaceType` | — |
| `url` | `jobUrl` / `applyUrl` | `hostedUrl` | `absolute_url` |
| body | `descriptionHtml`, in the list response | `descriptionPlain` + `lists[]`, must be joined | detail endpoint only |
| salary | `compensation`, structured | — | buried in prose, needs regex |

## Posting dates have different meanings

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

Ranking on the list endpoint alone puts Lyft, Block, Dialpad and Stripe first as the "freshest" roles. In that historical candidate set, the two GitLab postings were 7.7 and 7.8 days old; their update dates happened to agree with their first-publication dates.

For Greenhouse, the detail endpoint exposes `first_published`, so every surviving candidate gets a detail fetch. Ashby documents `publishedAt` as the date a posting was **last published**, which can change after republication; Lever's `createdAt` is a creation timestamp. These fields should not be treated as interchangeable proof of an original publication date. See [Ashby's public API reference](https://developers.ashbyhq.com/docs/public-job-posting-api).

The current `enrich.py` output still places all three values under `first_published`. For Ashby and Lever, treat that as a legacy field name and inspect the source ATS. The scanner does not yet maintain a durable first-seen history, so it cannot reconstruct a posting's original publication when the source does not supply it. The figures above describe the dated run, not current openings or a general error rate.

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

# 2. Per-posting detail: source dates, JD signals, liveness -> out/enriched.json
python3 ats/enrich.py

# 3. Pull the application form fields for one posting
python3 ats/forms.py <application-url> --json form-fields.json
```

Useful variants:

```bash
python3 ats/scan.py --probe                      # only test which slugs are alive
python3 ats/scan.py --slug cohere --ats ashby    # single company
python3 ats/scan.py --ats greenhouse --max-age 30
python3 ats/scan.py --now 2026-08-26             # change the age-reference date; responses are still live
python3 ats/enrich.py --workers 1 --delay 1.5    # when rate limited
```

**The split is deliberate.** The scan stage touches only list endpoints and defaults to 40 threads. In the documented run, coarse filtering dropped about 95% of title matches; adjust concurrency for the provider and network. Detail endpoints are slow and rate-limit, so they are reserved for candidates that already survived filtering. `enrich.py` defaults to 8 threads with backoff.

## Optional agent layer

[`agent/`](agent/) is an experimental wrapper that exposes the scanner through 8 tools for Claude. It is intended to choose which boards to inspect and when to stop. The repository does not include a recorded successful agent run or evaluation results; inspect its model ID and SDK settings before a paid run.

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

通过 Ashby / Lever / Greenhouse 三家官方公开接口扫描招聘看板，归一字段，并检查岗位详情和日期来源。

扫描层使用 Python 标准库，无需浏览器自动化或第三方 Python 包；可选 agent 层另需安装依赖。

日期语义需要分别处理：Greenhouse 的 `updated_at` 是更新时间，详情中的 `first_published` 才是首次发布；Ashby 的 `publishedAt` 是最近一次发布，Lever 的 `createdAt` 是创建时间。2026-08-28 那次 28 条候选中，15 条的年龄按更新时间计算被低估了 3 倍以上。这是一次历史观察，不代表当前岗位或总体错误率。

另外三个坑同样是踩出来的：Python 的 SSL 证书问题让首轮 393 个看板**静默全灭**（`live=0`，输出看着完全正常）；Greenhouse 的 404 会返回结构完整的 JSON，导致 444 个「在线」看板里有 328 个是幽灵——**这个 bug 没污染任何岗位数据，只错了统计量，因此最难发现**；以及 HTML 实体与标签的解码顺序。

完整技术笔记见 [`ats/NOTES.zh.md`](ats/NOTES.zh.md) 与 [`agent/NOTES.zh.md`](agent/NOTES.zh.md)。

