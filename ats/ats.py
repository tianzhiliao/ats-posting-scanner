#!/usr/bin/env python3
"""ATS 公开接口访问层 —— Ashby / Lever / Greenhouse。

三家 ATS 都把「公司看板」做成了公开只读 JSON 接口，不需要 key、不需要登录、
不需要浏览器。所以这里没有 Selenium / Playwright，全部是 HTTP GET + json.loads。
这也是为什么它比爬 LinkedIn 快两个数量级：一次请求拿一整个公司的全部在挂岗位。

    Ashby       GET  https://api.ashbyhq.com/posting-api/job-board/{slug}
    Lever       GET  https://api.lever.co/v0/postings/{slug}?mode=json
    Greenhouse  GET  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs

「爬取」的实质是**猜 slug**：公司在 ATS 上的短名（cohere / gitlab / instacart）。
猜中了接口返回 200 + 岗位数组，猜错了返回 404。所以流程是拿一个几百家公司的
候选 slug 表去批量探测，命中的就是一个可抓的看板。参见 companies.txt。
"""

import concurrent.futures as cf
import datetime
import html
import json
import re
import subprocess
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"

# 参照时间。默认当天，但复现历史结果时可以覆盖成当时的日期。
NOW = datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------- HTTP


class FetchError(Exception):
    pass


def _via_curl(url, timeout, data=None, headers=None):
    # -w 把状态码追加到 body 末尾。必须自己判状态码：curl 不加 -f 时 4xx 也算成功，
    # 而 Greenhouse 对不存在的看板返回 **404 + 合法 JSON**：{"status":404,"error":"Job not found"}。
    # 不判状态码的话 json.loads 会成功，d.get("jobs",[]) 得到 []，
    # 于是「看板不存在」被静默误判成「看板在线但 0 个岗位」——命中率虚高。
    # （Ashby 404 回纯文本、Lever 404 回非数组 JSON，各自会被下游挡住；只有 Greenhouse 会漏。）
    cmd = ["curl", "-sL", "--max-time", str(timeout), "-w", "\n%{http_code}",
           "-H", f"User-Agent: {UA}"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["-d", data]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise FetchError(f"curl exit {p.returncode}: {p.stderr.decode()[:120]}")
    body, _, code = p.stdout.decode("utf-8", "replace").rpartition("\n")
    if not code.startswith("2"):
        raise FetchError(f"HTTP {code}: {body[:80]}")
    return body


def _via_urllib(url, timeout, data=None, headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data.encode() if data else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch(url, timeout=25, data=None, headers=None):
    """取文本。优先 curl，失败再退回 urllib。

    顺序是反直觉的 —— 通常大家先用 urllib。但 macOS 自带 / python.org 装的 python3
    常常没配 CA bundle，urllib 会直接抛：

        URLError <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]
                  certificate verify failed: unable to get local issuer certificate>

    而系统 curl 用的是 keychain 里的根证书，同一个 URL 返回 200。
    2026-08-26 那次扫描第一轮就栽在这上面：393 个看板全部「探测失败」，
    live=0，看起来像是所有公司都没有看板，其实一个请求都没发出去。
    curl 在前、urllib 兜底，是从那次事故来的。
    """
    try:
        return _via_curl(url, timeout, data, headers)
    except (FetchError, FileNotFoundError, OSError):
        return _via_urllib(url, timeout, data, headers)


def fetch_json(url, timeout=25, data=None, headers=None):
    raw = fetch(url, timeout, data, headers)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise FetchError(f"non-JSON response ({len(raw)}B): {raw[:100]!r}") from e


def http_status(url, timeout=20):
    """只回状态码，用于存活性复查（岗位是否已下架）。"""
    p = subprocess.run(
        ["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout), url],
        capture_output=True,
    )
    return p.stdout.decode().strip()


# ---------------------------------------------------------------- 工具


def strip_html(s):
    """JD 正文都是 HTML 片段，转成纯文本才好跑正则。

    顺序要紧：**先解实体，再去标签**。Greenhouse 的 content 是实体编码的 HTML
    （`&lt;div&gt;…`），先去标签的话一个 `<` 都匹配不到，解完实体标签就全漏进正文了
    —— 症状是信号里出现 `3+ years in speech synthesis (<span data-highlig` 这种。
    末尾再解一次，处理标签内文本里的实体。
    """
    t = html.unescape(s or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def days_ago(value, now=None):
    """把三家各自的时间格式统一成「距今天数」。

    Ashby      publishedAt   ISO8601 带 Z        2026-08-07T14:22:01.000Z
    Greenhouse first_published ISO8601 带偏移    2026-08-20T09:15:00-04:00
    Lever      createdAt     epoch 毫秒整数      1786291200000
    """
    if not value:
        return None
    now = now or NOW
    s = str(value).replace("Z", "+00:00")
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.datetime.fromtimestamp(int(value) / 1000, datetime.timezone.utc)
        except (ValueError, TypeError, OSError):
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return round((now - d).total_seconds() / 86400, 1)


# ---------------------------------------------------------------- 匹配规则

# 岗位标题白名单。写得宽是故意的：先粗筛出「像 AI 工程」的，
# 精筛留给后面的 enrich 阶段看 JD 正文。
TITLE_RE = re.compile(
    r"(applied\s+(ai|ml|machine\s*learning|research)"
    r"|\bai\s+engineer|\bai/?ml\s+engineer|machine\s+learning\s+engineer"
    r"|\bml\s+engineer|\bllm\s+engineer|gen(erative)?\s*ai\s+engineer"
    r"|ai\s+software\s+engineer|software\s+engineer,?\s*(ai|ml|machine)"
    r"|forward\s+deployed|ai\s+solutions?\s+engineer"
    r"|member\s+of\s+technical\s+staff)",
    re.I,
)

# 资深标识黑名单。命中即排除（本工作区只找初/中级）。
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|director|manager|head\s+of|vp"
    r"|vice\s+president|distinguished|architect|iii|iv|\bl[456]\b|expert)\b",
    re.I,
)

# 加拿大信号：国名 + 主要城市 + 省名/省缩写。
CA_RE = re.compile(
    r"(canada|canadian|toronto|vancouver|montr|ottawa|calgary|waterloo|kitchener"
    r"|edmonton|halifax|quebec|winnipeg|victoria,?\s*bc|ontario|british\s+columbia"
    r"|alberta|\bon,\b|\bbc,\b|\bqc,\b)",
    re.I,
)

# 泛北美/全球远程。很多岗写 "Remote - North America"，不点名加拿大但加拿大可投。
REMOTE_GLOBAL_RE = re.compile(
    r"(remote\s*[-–—:,]?\s*(north\s+america|americas|global|worldwide|anywhere"
    r"|us\s*(&|/|and)\s*canada|na\b)|north\s+america|americas"
    r"|global\s*[-–—]?\s*remote|anywhere)",
    re.I,
)


def classify(title, loc):
    return {
        "senior": bool(SENIOR_RE.search(title or "")),
        "ca": bool(CA_RE.search(loc or "")),
        "na": bool(REMOTE_GLOBAL_RE.search(loc or "")),
    }


# ---------------------------------------------------------------- 三个适配器
#
# 每个适配器把一家 ATS 的看板拉平成同一张表：
#   ats company title loc emp remote posted age url dept jid
# 后面的筛选、富化、报告全部只认这张表，不再关心数据来自哪家。


def scan_ashby(slug, title_re=TITLE_RE, now=None):
    """Ashby Posting API。

    GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
      → {"jobs": [{title, location, secondaryLocations[], department, team,
                   employmentType, isRemote, publishedAt, jobUrl, applyUrl,
                   descriptionHtml, descriptionPlain, compensation{...}}, ...]}

    三家里最好用的一个：列表接口就直接给全文 JD + 薪资 + isRemote 布尔位，
    抓完这一个请求基本不用再翻详情页。
    """
    d = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    jobs = d.get("jobs", [])
    out = []
    for j in jobs:
        t = j.get("title", "")
        if title_re and not title_re.search(t):
            continue
        locs = [j.get("location", "")] + [
            s.get("location", "") for s in (j.get("secondaryLocations") or [])
        ]
        loc = " | ".join(l for l in locs if l)
        rec = dict(
            ats="ashby", company=slug, title=t, loc=loc,
            emp=j.get("employmentType"), remote=j.get("isRemote"),
            posted=j.get("publishedAt"), age=days_ago(j.get("publishedAt"), now),
            url=j.get("jobUrl") or j.get("applyUrl"),
            dept=j.get("department"), jid=j.get("id"),
        )
        rec.update(classify(t, loc))
        out.append(rec)
    return len(jobs), out


def scan_lever(slug, title_re=TITLE_RE, now=None):
    """Lever Postings API。

    GET https://api.lever.co/v0/postings/{slug}?mode=json
      → [{text, categories{location, allLocations[], commitment, department, team},
          workplaceType, createdAt(ms), hostedUrl, applyUrl, descriptionPlain,
          lists[{text, content}], additionalPlain}, ...]

    注意：顶层直接是数组，不是 {"jobs": [...]}；标题字段叫 text 不叫 title；
    createdAt 是 epoch 毫秒。三家里字段命名最不一样的一个。
    """
    d = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(d, list):
        raise FetchError(f"unexpected shape: {str(d)[:80]}")
    out = []
    for j in d:
        t = j.get("text", "")
        if title_re and not title_re.search(t):
            continue
        c = j.get("categories") or {}
        loc = " | ".join(filter(None, [c.get("location")] + (c.get("allLocations") or [])))
        rec = dict(
            ats="lever", company=slug, title=t, loc=loc,
            emp=c.get("commitment"), remote=j.get("workplaceType"),
            posted=j.get("createdAt"), age=days_ago(j.get("createdAt"), now),
            url=j.get("hostedUrl"), dept=c.get("department"), jid=j.get("id"),
        )
        rec.update(classify(t, loc))
        out.append(rec)
    return len(d), out


def scan_greenhouse(slug, title_re=TITLE_RE, now=None):
    """Greenhouse Job Boards API。

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
      → {"jobs": [{id, title, location{name}, absolute_url, updated_at,
                   metadata[{name, value}], requisition_id}, ...]}

    ⚠️ 列表接口只有 updated_at，没有 first_published。
    updated_at 是「雇主上次动过这条记录」—— 改个错别字、刷新一下都会更新。
    用它判断「新岗」会把挂了半年的老岗当成昨天发的。
    真实发布日必须走详情接口，见 greenhouse_job()。这是本工作区筛选口径里
    「用 first_published 而非 updated_at」那条规则的由来。

    另外列表接口默认不含 JD 正文，加 ?content=true 可以要，但会显著变慢，
    所以扫描阶段不要，留到 enrich 阶段按 id 逐条取。
    """
    d = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    jobs = d.get("jobs", [])
    out = []
    for j in jobs:
        t = j.get("title", "")
        if title_re and not title_re.search(t):
            continue
        loc = (j.get("location") or {}).get("name") or ""
        meta = " ".join(
            str(m.get("value")) for m in (j.get("metadata") or []) if m.get("value")
        )
        rec = dict(
            ats="greenhouse", company=slug, title=t, loc=f"{loc} || {meta}",
            emp=None, remote=None,
            posted=j.get("updated_at"), age=days_ago(j.get("updated_at"), now),
            url=j.get("absolute_url"), dept=None, jid=j.get("id"),
        )
        rec.update(classify(t, loc))
        out.append(rec)
    return len(jobs), out


SCANNERS = {"ashby": scan_ashby, "lever": scan_lever, "greenhouse": scan_greenhouse}


# ---------------------------------------------------------------- 详情接口


def greenhouse_job(slug, job_id, questions=False):
    """单条岗位详情。这里才有 first_published / content / offices / metadata。

    加 ?questions=true 会一并返回申请表单的全部字段定义 —— 见 forms.py。
    """
    q = "?questions=true" if questions else ""
    return fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}{q}")


def ashby_board(slug):
    return fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    )


def lever_posting(slug, job_id):
    return fetch_json(f"https://api.lever.co/v0/postings/{slug}/{job_id}?mode=json")


# ---------------------------------------------------------------- 并发外壳


def scan_boards(targets, title_re=TITLE_RE, workers=40, now=None, on_board=None):
    """targets: [(ats, slug), ...] → (results, boardlog)

    40 线程是实测的甜点：再高这三家会开始 429，再低 800 个看板要跑十分钟。
    单个看板失败不影响整体 —— slug 猜错返回 404 是常态，不是异常。
    """
    results, boardlog = [], []

    def one(ats, slug):
        try:
            total, rows = SCANNERS[ats](slug, title_re, now)
        except Exception as e:  # 404 / 超时 / 非 JSON 都归到这
            return ("ERR", ats, slug, type(e).__name__, [])
        return ("OK", ats, slug, total, rows)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, a, s) for a, s in targets]
        for f in cf.as_completed(futs):
            status, ats, slug, info, rows = f.result()
            boardlog.append((ats, slug, status, info))
            results.extend(rows)
            if on_board:
                on_board(ats, slug, status, info, len(rows))
    return results, boardlog


def load_companies(path):
    """读 companies.txt。格式：[ashby] 段头 + 每行一个 slug，# 起始为注释。"""
    targets, ats = [], None
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                ats = line[1:-1].strip().lower()
                continue
            if ats in SCANNERS:
                for slug in line.split():
                    targets.append((ats, slug))
    return targets
