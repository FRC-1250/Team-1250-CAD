# CLAUDE.md — Onshape Drawing Export (FRC Team 1250)

Automation that exports Onshape drawings to PDF when they change, commits the PDFs to a repo, and links them to GitHub issues.

**Read `drawing-export-spec.md` first.** It is the design of record. This file is the operating manual: the traps, the rules, and the things that cost real money to learn.

---

## 🚨 The quota is the whole game

**EDU Educator plan: 2,500 API calls per YEAR, shared across the entire company (FRC1250).**
Not per user. Not per month. The whole team, one pool.

- Exhaustion returns **`402`** — a wall, not a throttle.
- ⚠️ **The reset date is UNRESOLVED and undocumented.** The Developer tab shows two contradictory fields: `Tracking start date: Feb 19, 2026` vs `Days into billing cycle: 315/365` (⇒ ~Sep 4). 168 days apart. Nobody knows which governs; `auth/limits/` and `help/Plans/developer.htm` define neither. **Ask api-support@onshape.com.**
- **Assume the LATER date (~Feb 19, 2027).** The asymmetry is one-directional: assume-early/actually-late ⇒ run dry mid-season; assume-late/actually-early ⇒ free bonus. **Never spend against an assumed reset.**
- **It doesn't matter for design.** ~600–1,200/cycle projected vs 2,500 ⇒ ~2× headroom under *both* readings. Don't let this question block anything.
- Projected steady-state use is ~600–1,400/cycle. This tool is the team's dominant API consumer.
- Safety net: Onshape emails all admins at **25/50/75/100%** of usage.

**Rules — not suggestions:**

| Rule | Why |
|---|---|
| **Never call the API to explore, diagnose, or "just check."** Use the Glassworks Explorer (below) — free. | Discovery is the easiest way to burn a season. |
| **Never write code that polls on a timer.** Runs are **user-initiated**. | Hourly polling ⇒ ~6,500/yr ⇒ over quota. |
| **Develop against `fixtures/`, never live.** | Fixtures already caught 2 real bugs at zero cost. |
| **Log every API call** to `call_log` (2xx/3xx ⇒ counted, 4xx/5xx ⇒ free). | Onshape exposes usage only in company settings, never via API. |
| **On `402`: halt and shout. Never retry.** | Retrying cannot help and masks a team-wide event. |

### The Explorer is free — the API key is not

> **Exempt:** *"Calls made from the Onshape browser, mobile clients, or the Onshape API Explorer (when authenticated via an Onshape session)"*
> **Counted:** *"Calls made from the Onshape API Explorer when authenticated via API keys or OAuth2"*

`https://cad.onshape.com/glassworks/explorer/` — signed into Onshape in another tab, **do not click _Authorize_**. Same call, same data, zero cost. Clicking Authorize flips it to billable.

**Claude cannot run these.** Session auth means a human with a browser. Claude has no Onshape credentials and the documents are private (verified: `403`/`401`). Experiments requiring writes (creating a version) are the user's call on their production data. **Ask; never fabricate a result.**

---

## ⚠️ Traps — every one of these failed SILENTLY

This project's characteristic failure is **no error, no output, looks healthy**. Four found so far:

| Trap | Reality |
|---|---|
| **`elementType=DRAWING` returns `[]`** | Drawings are **`elementType: "APPLICATION"`, `dataType: "onshape-app/drawing"`**. `DRAWING` is a legal enum value that no drawing reports. The query param is an **unvalidated bare string** — Onshape never rejects a wrong value, it just returns `[]`. |
| **`defaultWorkspace.microversion` never moves** | It's the workspace's **creation** microversion. Unchanged across 18 versions. As a change signal it would never fire. Use `/documents/d/{did}/w/{wid}/currentmicroversion`. |
| **`deleted` is in the schema, absent from the response** | `element["deleted"]` ⇒ `KeyError` on every element. Use `.get("deleted", False)`. **The v16 schema says what _may_ be present, not what _is_.** Treat all optional fields as absent-by-default. |
| **The 2021 spec** | `github.com/onshape-public/onshape-clients` is **5 years stale** (spec 1.113, last commit 2021-11-15). An early draft of the design was built on it. **Never use it.** |

**Source of truth: `https://cad.onshape.com/api/v16/openapi`** (live, `1.217.82698`). Free to download — not an authenticated API call. Paths have **no `/api` prefix**.

**When Claude's claim and the Explorer disagree, the Explorer wins.** This has already happened.

---

## Verified constants — do not guess these

```python
DRAWING_ELEMENT_TYPE = "APPLICATION"            # NOT "DRAWING"
DRAWING_DATA_TYPE    = "onshape-app/drawing"    # required: APPLICATION also matches CAM Studio
CHASSIS_DID          = "9277e520a8289c72778da2ae"
CHASSIS_WID          = "7b7c1c4a728bdb6e94ce23eb"
FOLDER_PARENT_ID     = "2b21f048c6c582e58dd11553"
COMPANY_ID           = "68b9b68c8ebe03eeb23bbe39"   # FRC1250
```

Confirmed behaviour:
- `versions[-1]` is **newest** (list is ascending by `createdAt`; `limit=1` returns the **oldest**).
- Creating a version **bumps `document.modifiedAt`** but **not** `workspace.modifiedAt`. Proven by experiment.
- `defaultWorkspace.parent` == newest version id.
- Translation path is `/drawings/d/{did}/{wv}/{wvid}/…` — **`{wv}` accepts a version**. Every doc page shows only `/w/{wid}` and is misleading. This claim carries the whole architecture.
- `purpose: 1` ⇒ Onshape **auto-version** (external-reference refresh), skip. *Inferred from 2 samples; no enum in spec — log skips.*

---

## Part number convention

```
<team>-<YY><bot>-[A]<subsystem><part>

1250-26B-101     team 1250, 2026 B-bot, subsystem 1 (chassis), part 01
1250-26A-A503    subassembly drawing (A prefix), subsystem 5, drawing 03
```

```python
PATTERN = re.compile(r'(?P<id>(?P<team>\d{3,5})-(?P<yy>\d{2})(?P<bot>[A-Z])-(?P<asm>A?)(?P<sn>\d{3,4}))')
```

Use `.search()`, not `.match()` — names may carry trailing descriptions. **The full `id` group is the match key**; no decomposition needed for issue linking.

**The regex GATES export.** Drawings that don't match are not exported at all — deliberately, to enforce the numbering scheme. Consequences:
- **Every skip must be logged, with the name and a per-run count.** *"Nothing exported"* must never be confusable with *"nothing changed."*
- Filenames come from the **extracted identifier**, never the raw name. Real tab names contain literal `"` (inch marks: `Tube 2"x1"x18.5" Drawing 1`) — illegal in Windows filenames.
- As of 2026-07-16, **zero** drawings conform. The chassis correctly exports nothing. This is expected, not a bug.

> ⚠️ Known ambiguity: subsystem is 1–2 digits, part assumed 2 (`sn[:-2]` / `sn[-2:]`). If part numbers can be 3 digits, `1101` is ambiguous. Harmless for matching (whole string); blocking only if subsystem grouping is added.

---

## Environment

- **Python 3.13.5** at `AppData\Local\Programs\Python\Python313`. **Invoke as `py`, not `python`** — `python` hits a Microsoft Store App Execution Alias (a 0-byte redirector, not an interpreter) because `Python313\` isn't on PATH.
- `sqlite3` 3.49.1 (stdlib). **`requests` is NOT installed** — prefer stdlib `urllib.request` to keep the project dependency-free for students who clone and run.
- ⚠️ **Console is `cp1252`, not UTF-8.** Printing `→` or `…` raises `UnicodeEncodeError` and **crashes the run**. Keep console output **strictly ASCII**; pass `encoding="utf-8"` explicitly on all file I/O.
- Git 2.50.1 present. **`gh` is NOT installed** (`winget install GitHub.cli`).
- Not a git repo yet.

---

## Architecture

```
Stage A: Onshape -> PDF     quota-constrained (2,500/yr, shared, fragile)
              |  SQLite (export.output_path, status=DONE)
Stage B: PDF -> git commit -> GitHub issue comment    unconstrained (5,000/hr)
```

**Keep these decoupled.** SQLite is the seam. A GitHub failure must never burn Onshape quota on retry; Stage B re-runs freely against PDFs already paid for.

Detection cascade (spec §4.2): ~~Stage 0~~ → Stage 1 version gate (`purpose`, no-op microversion) → Stage 2 per-drawing `microversionId` + regex gate → Stage 3 export. Most gates are free — they run on already-fetched data.

**Stage 0 is disabled** (`--stage0` to opt in). It works, but the folder holds ~40–60 documents, not 5, so it costs **3 paginated calls, not 1** (`/documents` caps `limit` at 20 — undocumented; 50 ⇒ 400). Break-even at best, and it makes `--doc` subset scanning 4× worse. Re-enable only if the subsystem docs move to their own ≤20-entry folder.

**`--doc <key>` is the main quota lever**: 1 call instead of 5. Use it whenever you know what changed.

**Measured costs (first live run, 2026-07-16):** full export of one drawing = **6 calls** (versions 1, elements 1, translate 1, poll 2, download 1). No-op run = **1 call/doc**.

---

## Working agreements

- **Verify against fixtures or live v16 — never from memory.** Every constant in this file was wrong at some point.
- **Mark inferences as inferences.** `purpose: 0/1` rests on two samples. Log it; don't trust it silently.
- **Make failures loud.** Given four silent-failure traps, prefer a noisy skip to a quiet one. A misnamed drawing that never exports and never complains is the worst outcome this tool has.
- **Ask before writes to Onshape.** Creating a version mutates a production document the whole team's top-level assembly references.
- **Capture new API responses into `fixtures/`.** They're free, they're ground truth, and they've already outperformed the spec twice.

## Layout

```
drawing-export-spec.md      design of record
CLAUDE.md                   this file
fixtures/
  CAPTURE.md                Explorer capture protocol (0 quota)
  response_1-1.json         getDocument (before V18)
  response_1-2.json         getDocumentVersions (17 versions)
  response_1-3.json         elements @version, elementType=DRAWING -> [] (the trap)
  response_1-4.json         elements @workspace, elementType=DRAWING -> [] (the trap)
  response_1-5.json         elements @workspace, UNFILTERED -> ground truth
  response_3-5.json         getDocument (after V18) -- proves modifiedAt bumps
```

## Open (non-API)

1. The other **5 document URLs**.
2. **Anything else on the team using the API?** The 2,500 is company-wide.
3. The **repo** for PDFs; real **issue titles** to match against.
4. **Closed issues** — comment or skip?
