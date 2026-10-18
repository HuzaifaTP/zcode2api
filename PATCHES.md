# Local patches — `zcode-hub` (fork of dengyie/zcode2api)

Base commit: `bda8ea1` — *feat: 新指纹逻辑收口(2.6.8)* (2026-10-02)

Everything below is **our local work**, not upstream. Each entry says what it fixes and
whether it is a candidate for upstreaming (so that when upstream fixes the same bug we
can drop our hunk during a rebase).

---

## 1. Captcha solver could never launch — `captcha_node/solver_pw.js`

**Upstream candidate: yes (breaks the product on current Chromium).**

`solveOnce()` passed `--single-process` to `puppeteer.launch()`. Chromium 155 refuses to
start with that flag, so every solve failed and `/v1/messages` returned
`captcha_error` forever.

Measured A/B (same image, same binary):

```
OK    []                                        Chrome/155.0.8059.79
FAIL  ["--single-process"]
OK    ["--no-zygote"]
FAIL  ["--single-process","--no-zygote","--renderer-process-limit=1"]
```

Fix: drop `--single-process`, keep `--no-zygote`.

---

## 2. `prompt_tokens` under-reported by up to 200× — `app/openai_compat.py`

**Upstream candidate: yes.**

Anthropic/GLM report `input_tokens` as the **non-cached** portion only; the cached prefix
arrives separately in `cache_read_input_tokens`. The OpenAI translation emitted
`prompt_tokens = input_tokens`, so a warm 8,039-token prompt reported **35**.

Consequences: clients doing context accounting overfill and eventually get a hard 400;
cost dashboards under-report by the cache-hit ratio.

Fix: `prompt_tokens = input_tokens + cache_read + cache_creation`, and emit
`prompt_tokens_details.cached_tokens`. Applied to both the non-stream and stream
(`StreamConverter`) paths.

---

## 3. HTTP 200 error envelopes treated as success — `app/routes/gateway.py`

**Upstream candidate: yes.**

GLM returns quota failures as `200 OK` with a business envelope:

```json
{"code":1005,"msg":"exceed quota limit"}
```

The success branch accepted any 200 without inspecting the body, so the error JSON was
passed to the client as if it were a message, monitoring recorded `ok=True`, and the
account was never marked.

Fix: for non-SSE responses, read and validate the body. Quota codes (`1005`, or
`EXHAUST_KEYWORDS` matches) mark **that model** exhausted on **that account** and retry
the next account. Risk codes apply the normal cooldown. Anything else returns a real
`502` with the upstream code. The validated bytes are replayed to the client via
`_Upstream.buffered`, so the response is unchanged.

---

## 4. Per-model exhaustion was invisible to the scheduler — `app/models.py`, `app/store.py`

**Upstream candidate: yes.**

`Account.status` is account-wide, but quota is per model. An account with
`GLM-5.3-Flash = 0` and `GLM-5.3 = 3,000,000` stayed `active`, so round-robin kept
picking it for Flash requests and every one failed — starving healthy accounts.

Fix: `Account.model_exhausted: {model: until_ts}` with
`is_model_exhausted() / mark_model_exhausted() / clear_model_exhausted()`;
`is_selectable(model=)` and `store.select(model=)` skip exhausted models. A successful
call clears the entry, so recovery is immediate.

Window: `ZCODE_MODEL_EXHAUST_SECONDS`, default 1800.

---

## 5. Anthropic route recorded no usage at all — `app/routes/gateway.py`

**Upstream candidate: yes.**

`/v1/messages` is a byte passthrough (`_Upstream.to_streaming` → `aiter_bytes`), so
`input_tokens` / `output_tokens` were always `null` in monitoring — for the endpoint most
clients actually use.

Fix: `to_streaming(req_id, stream=...)` sniffs usage on the way out. Non-stream keeps the
JSON body; stream scans SSE lines and retains only usage-bearing ones (bounded memory).
Bytes still reach the client untouched.

---

## 6. Monitoring history lost on every restart — `app/reqlog.py`

**Upstream candidate: yes.**

The ring buffer was memory-only (`deque(maxlen=500)`), so every container rebuild wiped
the page.

Fix: append each finished entry as one JSON line to `data/monitoring.jsonl` (no fsync —
observability does not need crash-level durability), restore the last 500 lines on import,
and truncate the file to its newest half once it exceeds
`ZCODE_MONITORING_MAX_BYTES` (default 8 MB). `clear()` deletes the file.
Toggle: `ZCODE_MONITORING_PERSIST=0`.

Also added `cached_tokens` to the record and to `finish_ok()`.

---

## 7. Frontend: Chinese → English

**Upstream candidate: maybe (upstream is a Chinese project; they may not want it).**

2,361 Chinese characters translated across `frontend/admin/*.html` and `frontend/js/*.js`
(~275 UI strings + 33 comments), plus full-width punctuation converted to ASCII.

Gotcha worth remembering: a translation inserted `account's` inside a **single-quoted JS
string**, which broke the whole inline script — header never rendered and the account list
was empty. Any future string edit must escape `'` or avoid apostrophes.

---

## 8. Frontend: cache-hit visibility

**Upstream candidate: yes.**

New 「缓存命中」 stat card (hit-rate %) and a **Cache** column in the live request table,
plus the 10th grid column in `frontend/css/app.css`.

---

## 9. Frontend: number formatter showed `2700.00 K`

**Upstream candidate: yes (it was a unit bug introduced by the translation).**

The Chinese units were `万` (10⁴) and `亿` (10⁸). Mapping them literally to `K`/`B` while
keeping the divisors produced `2700.00 K` for 2.7 M tokens.

Fix: proper SI steps — `1e9→B`, `1e6→M`, `1e3→K` — and `toLocaleString('en-US')`.

---

## 10. Frontend: nav text invisible in some browsers

**Upstream candidate: yes (defensive).**

`.admin-header` used `backdrop-filter: blur(10px)` over a translucent background — a
known Chromium compositing path where child text can disappear. Header is now solid
`#16171b`.

`.admin-nav-link` also got literal colour fallbacks (`color: var(--ink-mute, #838794)`),
`-webkit-text-fill-color`, and explicit `opacity`/`visibility`/`white-space`, which
covers forced-dark extensions and unresolved `var()`.

`frontend/version` is bumped on every frontend change — it is the cache-buster for
`app.css?v=` / `header.js?v=`.

---

## 11. Deployment (local only, never upstream)

- `Dockerfile` — Python 3.13 + Node 20 + **Chromium** (needed by `solver_pw.js`), and
  `npm install` instead of `npm ci` because upstream's `package-lock.json` is out of sync
  with `package.json` (lock says 2.0.0/happy-dom only, package.json says 2.1.0 +
  puppeteer-core).
- `docker-compose.yml` — binds `127.0.0.1:3003`, mounts `./data`, sets admin/gateway keys.
  Contains a commented-out US-proxy block: **`/v1/messages` needs a clean egress IP** or
  the upstream answers `3012 unusual activity` (HTTP 405). Billing/quota stay direct
  because `app/claim.py` pins `trust_env=False`.

---

## Updating from upstream

```bash
git fetch origin
git rebase origin/master
```

If a hunk is already fixed upstream, take theirs (`git checkout --theirs <file>`) and drop
the entry above. Re-apply the captcha flag fix (#1) first — without it nothing runs.
