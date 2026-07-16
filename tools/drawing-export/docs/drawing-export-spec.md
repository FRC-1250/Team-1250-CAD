# Onshape Drawing → PDF Export: Specification

**Status:** Design resolved — one open question (§4: trigger choice)
**Date:** 2026-07-16
**Context:** FRC robot builds. 5–6 main documents containing all subsystems. EDU license, 2,500 calls/yr, resets Feb 19.
**Goal:** Export Onshape drawings to PDF automatically, only when they change.

---

## 1. Resolved constraints

| Fact | Consequence |
|---|---|
| **No Release Management** (confirmed: no "Release"/"Revision History" on tab right-click) | **No revisions API.** `enumerateRevisions` is out. No `partNumber`, no `canExport`/`isTranslatable` pre-checks, no admin key needed. |
| **5–6 main documents** | **No folder scan.** Document IDs are a config list. `globaltreenodes` (undocumented) is out. |
| **EDU Educator plan** — 2,500 calls/yr **per _company_**, resets Feb 19 | **Shared team-wide pool.** Every call competes with any other team tooling. Reset lands mid-season (§6). |
| **User-initiated** scan, ~10×/week in season | **No cron, no polling burn.** But ~138 runs/yr × 6 docs is still the dominant cost — see §6. |
| **Versions are frequent, not release points** — created to publish changes to the top-level assembly | ⚠️ **A version does NOT imply a drawing changed.** Version-as-trigger alone would mass-re-export. Drives the hybrid in §4. |
| **A→B→C ⇒ export C only** | Latest-wins. Falls out of `versions[-1]` for free. |
| **Python 3.13.5 installed** | Invoke as `py`. See §10. |

**Everything in this design is verified against the live v16 spec** — see §2.

---

## 2. Verified API surface

**Source of truth:** `https://cad.onshape.com/api/v16/openapi` — live, **version `1.217.82698`**, re-verified 2026-07-16.

> ⚠️ **Do not use `github.com/onshape-public/onshape-clients/openapi.json`.** That repo's last commit is **2021-11-15** and it pins spec version **1.113** — ~104 minor versions stale. An earlier draft of this document was built on it. Every claim below has since been re-verified against live v16; the corrections it produced are listed in §2.1.

Paths below are **exactly as the live spec and the Explorer show them — no `/api` prefix** (the base URL comes from the spec's `servers` block).

```
GET  /documents/{did}                                     → BTDocumentInfo (modifiedAt, defaultWorkspace)
GET  /documents?parentId={fid}&limit=…                    → folder contents, each w/ modifiedAt
GET  /documents/d/{did}/versions                          → all versions of a doc
GET  /documents/d/{did}/{wvm}/{wvmid}/elements            → ?elementType=APPLICATION  (NOT "DRAWING" — §4.6)
       └ BTDocumentElementInfo: id, name, elementType, dataType,
         microversionId, deleted, thumbnailInfo, …
         drawings ⇒ elementType=APPLICATION, dataType="onshape-app/drawing"
GET  /documents/d/{did}/{wv}/{wvid}/currentmicroversion   → doc-level microversion
POST /drawings/d/{did}/{wv}/{wvid}/e/{eid}/translations   → start PDF export
GET  /translations/{tid}                                  → ACTIVE|DONE|FAILED
GET  /documents/d/{did}/externaldata/{fid}                → PDF bytes
```

> **Critical, and contradicts every doc page:** the translation path segment is **`{wv}`**, not `{wid}` — it accepts a workspace **or a version**. Every doc page and forum post shows only `/w/{wid}`, implying versioned drawings can't be exported. They can. **Confirmed in live v16.** This is what makes version-based export possible at all — the single claim the whole architecture rests on.

### 2.1 Corrections from the v16 re-verification

| Item | Stale (1.113) | **Live v16 (1.217)** |
|---|---|---|
| Path prefix | `/api/documents/…` | **`/documents/…`** — no prefix |
| `elementType` on `getElementsInDocument` | `int32` (ordinal guess: DRAWING=2) | **`string`**, unvalidated. The int-vs-string ambiguity was a 2021 artifact. ⚠️ But the correct value is **`APPLICATION`**, not `DRAWING` — see §4.6. |
| `BTDocumentInfo` | flat object | polymorphic (`allOf` → `BTGlobalTreeNodeSummaryInfo`); fields unchanged |
| New fields | — | `BTDocumentElementInfo.deleted`, `BTDocumentInfo.hasReleaseRevisionableObjects`, `trash` |

**Every load-bearing claim held** (`{wv}`, `microversionId`, `storeInDocument`, `currentSheetOnly`, `parentId`, `modifiedAt`). Use `deleted` to skip tombstoned elements — it wasn't available in the old spec.

**Key find:** `BTDocumentElementInfo` carries a **per-element `microversionId`**. One `elements` call per document returns every drawing *and* its individual change fingerprint — drawing-level change detection for one call per document.

---

## 3. Export flow (trigger-independent)

```
1. POST /drawings/d/{did}/{wv}/{wvid}/e/{eid}/translations     1 call
      { "formatName": "PDF", "storeInDocument": false }
2. GET  /translations/{tid}   → poll w/ backoff until DONE      1–N calls
3. GET  /documents/d/{did}/externaldata/{fid}  → PDF bytes      1 call
      fid = resultExternalDataIds[0]
```

Confirmed `BTTranslateFormatParams` fields: `formatName`, `storeInDocument`, `currentSheetOnly`, `destinationName`, `versionString`, `configuration`, `notifyUser`, `emailTo`.

- `storeInDocument: false` — PDFs are **not** written back as blob tabs; fetched via `externaldata`. Keeps documents clean.
- `currentSheetOnly` — leave false/unset so multi-sheet drawings export as a single PDF.

---

## 4. Design: version trigger + microversion filter

### 4.1 Why a pure version trigger is wrong for you

You version to **make changes available to the top-level assembly** — not to mark a drawing finished. So most versions contain **no drawing change at all**. Triggering export on "new version" would re-export every drawing in a document every time anyone publishes upstream — on a shared 2,500 pool, that's the most expensive possible mistake.

**Versions answer "did anything happen?" — they don't answer "did *this drawing* change?"**

`BTDocumentElementInfo.microversionId` answers the second question, and it's returned by the same `elements` call we already need to enumerate drawings. So it costs nothing extra.

### 4.2 The cascade — cheapest gate first

```
STAGE 0 — ✅ PROVEN (§4.3)                                 1 call
  GET /documents?parentId=2b21f048c6c582e58dd11553
    → all 6 docs, each with modifiedAt
    → skip any doc whose modifiedAt hasn't advanced        # version creation bumps it

STAGE 1 — did this doc get a real new version?            1 call per surviving doc
  GET /documents/d/{did}/versions
    newest = versions[-1]                                  # ✅ ascending; [-1] is newest (§4.4)
    if newest.id == last_known_version[did]:      skip     # free
    if newest.purpose != 0:                       skip     # auto-version (§4.5a)
    if newest.microversion == last_exported_mv:   skip     # no-op version (§4.5b)

STAGE 2 — which drawings actually changed?                1 call per changed doc
  GET /documents/d/{did}/v/{newest.id}/elements?elementType=APPLICATION   # NOT "DRAWING" — §4.6
    → keep only dataType == "onshape-app/drawing"          # ✅ confirmed; excludes CAM Studio, BOMs
    → skip elements where el.get("deleted", False)         # ⚠️ field ABSENT from response — §4.7
    → skip elements whose name fails the ID regex          # §4.8 — LOG EACH ONE
    → export ONLY where microversionId != last_exported_mv[element_id]

STAGE 3 — export                                          ~5 calls per changed drawing
  POST /drawings/d/{did}/v/{vid}/e/{eid}/translations → poll → download
```

**Five of the six gates are free** — they run on data already fetched. Only Stage 2 and Stage 3 spend calls, and only for documents that got a genuine, content-changing, human-made version containing a conformingly-named drawing that actually moved.

**Stage 2 is the money-saver.** It's the difference between "someone versioned the drivetrain doc, re-export all 12 of its drawings" (~60 calls) and "one drawing moved, export one" (~5 calls). Given your versioning pattern, this likely cuts export spend by most of its volume.

Properties:
- ✅ **A→C jump free** — `versions[-1]` skips intermediates by construction.
- ✅ **Immutable source** — exports from `/v/{vid}`, never catches a mid-edit drawing.
- ✅ **No new team habits** — works with how you already version.
- ✅ **No debounce needed** — versions are already deliberate acts.
- ✅ **Zero unverified parameters** — every endpoint, param, filter value and field confirmed against live v16 or a real response (§8.0a).

### 4.3 Stage 0 — proven, then ❌ DISABLED BY DEFAULT (live run, 2026-07-16)

**The mechanism works. The economics don't.** Stage 0 is now opt-in via `--stage0`.

> **Why:** the design assumed the folder held ~6 documents ⇒ 1 call to gate them all. **It holds ~40–60** — it's the team's whole CAD library. `GET /documents` caps `limit` at **20** (undocumented; `limit=50` ⇒ `400`), so gating costs **3 paginated calls, not 1**.

| Scenario | With Stage 0 | Without | Verdict |
|---|---|---|---|
| Nothing changed | 3 | 5 | saves 2 |
| 1–2 docs changed (typical) | 4–5 | 5 | ~break-even |
| All 5 changed | **8** | 5 | **costs 3** |
| **`--doc chassis`** (§4.9) | **4** | **1** | **4× worse** |

It breaks even at best, loses when the team is busy, and **quadruples the cost of subset scanning** — the single most effective lever. Default is now **off**.

**Re-enable if** the 5 subsystem docs are ever moved into their own folder (≤20 entries ⇒ 1 call ⇒ genuinely worth it). The code and the proof below remain valid; only the folder's contents made it uneconomic.

#### The experiment (still valid, now a regression test)

**Creating a version DOES bump `document.modifiedAt`:**

| | `document.modifiedAt` | `defaultWorkspace.modifiedAt` |
|---|---|---|
| Before V18 (`response_1-1`) | `12:16:13.270` *(= V17 creation)* | `12:18:43.243` |
| After V18 (`response_3-5`) | **`13:01:53.241`** *(= V18 creation)* | `12:18:43.243` *(unchanged)* |

Two conclusions, both load-bearing:

1. **`document.modifiedAt` tracks version creation** ⇒ Stage 0 is a safe gate. Saves ~400 calls/yr.
2. **`document.modifiedAt` does NOT track workspace edits** — the workspace was modified at 12:18:43 and the document's `modifiedAt` never followed. This is *desirable*: Stage 0 sees version events and ignores WIP noise. It also means `document.modifiedAt` must **never** be used to detect workspace changes.

**Folder ID confirmed:** `parentId = 2b21f048c6c582e58dd11553` (from `response_1-1`). `GET /documents?parentId=` is the **officially supported folder scan** — the original "scan a folder" ask, satisfied in one call, with no dependency on `globaltreenodes`.

**Bonus:** `defaultWorkspace.parent` moved V17 → V18 across the experiment. The workspace's parent **is** the newest version ID — a free cross-check, available from `getDocument` without calling `getDocumentVersions`.

> ⚠️ **Trap — do not use `defaultWorkspace.microversion`.** It reads `69e8f728b46c9db70b8810b1`, identical to the *"Start"* version from 2026-05-12, and `defaultWorkspace.createdAt` matches Start's timestamp. It is the workspace's **creation** microversion, not its current one — it did not move across 18 versions. As a change signal it would **silently never fire**. Current workspace state requires `/documents/d/{did}/w/{wid}/currentmicroversion`.

### 4.4 Version ordering — ✅ RESOLVED

`getDocumentVersions` exposes no sort parameter, but the observed list (`response_1-2`) is **ascending by `createdAt`**: `[0]` = "Start" (2026-05-12) … `[-1]` = "V17" (2026-07-16).

**`versions[-1]` is newest.** No client-side sort needed. *(Caveat: `limit`/`offset` paginate this ascending order, so `limit=1` returns the **oldest**. Don't use it to fetch "the latest".)*

### 4.5 Two free filters discovered in the fixtures

Both come from data Stage 1 **already fetches** — zero additional calls.

#### (a) `purpose != 0` ⇒ auto-version, skip

Of 17 versions, 15 are `purpose: 0` and **two are `purpose: 1`** — V4 and V13. Both carry the description *"Updated out of date external references from https://cad.onshape.com/documents/77ccfe85426b6dca10c64431/…"*.

These are **Onshape auto-versions**, created when out-of-date external references are refreshed — not human intent. (`defaultVersionGraphShowAutoVersions: true` on the document corroborates the concept.) They represent no design change; exporting on them is waste.

> **Strong inference, not documented.** `purpose` is an int32 with **no enum in the v16 spec**. The 0/1 meaning is inferred from these two samples. Treat `purpose != 0` as "probably auto" and **log skips** so a wrong guess is visible rather than silent.

#### (b) Identical microversion ⇒ no-op version, skip

```
V16  microversion: 4ac8a0c75f7a57ab9a0bc696
V17  microversion: 4ac8a0c75f7a57ab9a0bc696   ← identical
```

V17 was created with **zero content change**, and the versions list reveals it for free.

**This directly targets the §4.1 problem.** Versions are created to publish to the top-level assembly, not because content changed — so no-op versions are expected to be *common*. Skipping them costs nothing and avoids the `elements` call entirely.

```
if newest.microversion == last_exported.microversion:  skip   # no content change at all
if newest.purpose != 0:                                skip   # auto-version
```

This is a gate **above** Stage 2: it rejects versions before any further call. Stage 2's per-drawing `microversionId` filter remains necessary — a doc-level microversion change doesn't mean a *drawing* changed.

### 4.6 ⚠️ `elementType=DRAWING` is WRONG — drawings are `APPLICATION`

**Discovered by experiment, 2026-07-16.** `getElementsInDocument?elementType=DRAWING` returned `[]` against the chassis at both V17 and the workspace — a document containing **three drawings**.

`GBTElementType` genuinely contains `DRAWING`:
```
[PARTSTUDIO, ASSEMBLY, DRAWING, FEATURESTUDIO, BLOB, APPLICATION,
 TABLE, BILLOFMATERIALS, VARIABLESTUDIO, PUBLICATIONITEM, UNKNOWN]
```
…so the value is *legal but not what drawings report*. Onshape implements drawings as **application elements**; the tab's Properties dialog shows **Category = Drawing**. `DRAWING` in the enum appears to be legacy or used elsewhere.

✅ **CONFIRMED against `response_1-5.json`:**

```
elementType : "APPLICATION"
dataType    : "onshape-app/drawing"
type        : "Application"          ← human-readable; NOT "Drawing"
```

Real chassis contents (8 elements) and how the filter resolves them:

| name | elementType | dataType | drawing? |
|---|---|---|---|
| Chassis | `PARTSTUDIO` | `onshape/partstudio` | — |
| 100 - Chassis | `ASSEMBLY` | `onshape/assembly` | — |
| `Tube 2"x1"x18.5" Drawing 1` | **`APPLICATION`** | **`onshape-app/drawing`** | **YES** |
| `Tube 2"x1"x19.5" Drawing 1` | **`APPLICATION`** | **`onshape-app/drawing`** | **YES** |
| `Tube 1"x1"x27" Drawing 1` | **`APPLICATION`** | **`onshape-app/drawing`** | **YES** |
| Bumper | `ASSEMBLY` | `onshape/assembly` | — |
| BOM : 100 - Chassis | `BILLOFMATERIALS` | `onshape/billofmaterials` | — |
| BOM : Bumper | `BILLOFMATERIALS` | `onshape/billofmaterials` | — |

Note `type: "Application"` — the human-readable type is **not** "Drawing" either. Drawings also carry a populated `applicationTarget.baseHref` (`…production-drawing-usw2c….onshape.com/editor`), a secondary indicator; `dataType` is the cleaner discriminator.

> **Why this mattered more than a typo:** the wrong constant returns `[]` — not an error. Every run would report "no changes," export nothing, and look perfectly healthy. It was found only because the fixture disagreed with the document. **The query param is an unvalidated bare `string`** (`"type": "string", "default": ""`) — the v16 spec applies the `GBTElementType` enum only to the *response* field, so Onshape will never reject a bad filter value.

⚠️ **`APPLICATION` is broader than drawings.** Your Educator plan includes **CAM Studio**, whose tabs are also `APPLICATION` elements. The `dataType` check is **required**, not cosmetic — without it, the exporter would try to translate CAM tabs as PDFs.

### 4.7 ⚠️ `deleted` is in the schema but NOT in the response

The v16 `BTDocumentElementInfo` schema declares `deleted: boolean`. **The actual response omits it entirely.** Observed keys on a real drawing element:

```
accelerationUnits, angleUnits, angularVelocityUnits, applicationTarget, areaUnits,
dataType, densityUnits, elementType, energyUnits, filename, forceUnits, foreignDataId,
frequencyUnits, id, lengthUnits, massUnits, microversionId, momentUnits, name,
pressureUnits, prettyType, safeToShow, specifiedUnit, thumbnailInfo, thumbnails,
timeUnits, type, unupdatable, volumeUnits, zip
```

No `deleted`. **`element["deleted"]` would raise `KeyError` on every element.** Use `element.get("deleted", False)`.

**General rule for this codebase: the v16 schema describes what *may* be present, not what *is*.** Treat every optional field as absent-by-default. Fields present in the response but *not* in my earlier reading of the schema: `safeToShow`, `prettyType`, `zip`, `unupdatable`, `applicationTarget`, `type`.

### 4.8 The ID regex is a GATE, not just a matcher

**Decision (2026-07-16): drawings whose tab name fails the §5b.2a regex are not exported at all.**

Previously the regex only chose *which issue to link*; a non-conforming drawing would still export and land as `NO_MATCH`. Now it decides *whether to export*. Three consequences:

1. **Saves quota.** Non-conforming drawings never reach Stage 3 (~5 calls each). Currently *all three* chassis drawings fail the regex (e.g. `Tube 2"x1"x18.5" Drawing 1`), so today this document would cost **0** export calls.
2. **Removes the filename-sanitization problem.** Output is named from the *extracted identifier* (`1250-26B-101.pdf`), never the raw tab name. This matters: `Tube 2"x1"x18.5" Drawing 1` contains **literal double quotes** — illegal in Windows filenames (`\ / : * ? " < > |`), and inch marks will be routine in your part names. Gating on the regex means an illegal filename can never be constructed.
3. ⚠️ **Introduces a silent-failure risk — must be mitigated.** A drawing that *should* export but is misnamed is now skipped with no error. Given this project's history (`elementType=DRAWING` → `[]`; `defaultWorkspace.microversion` → frozen), **every skip must be loud**:

```
run summary:
  exported: 0
  skipped (name doesn't match convention): 3
    - 'Tube 2"x1"x18.5" Drawing 1'   (chassis)   mv=46e3d634...
    - 'Tube 2"x1"x19.5" Drawing 1'   (chassis)   mv=1b6c0b5d...
    - 'Tube 1"x1"x27" Drawing 1'     (chassis)   mv=209d7039...
  -> rename to <team>-<YY><bot>-[A]<S><NN> to include these
```

*(Real names from `response_1-5.json` — this is the chassis's actual output today. Note the ASCII arrow: console is cp1252, §10.)*

Per-run counts + names, so "nothing exported" is always distinguishable from "nothing changed". **Never** let a skip pass unreported.

### 4.9 Subset scanning — the cheapest lever you control

Since the scan is user-initiated, allow scoping it:

```
py export.py                       # all 6 docs      ~6 calls
py export.py --doc drivetrain      # one doc         ~1 call
py export.py --since 2026-01-15    # skip stale docs
```

When you know only the drivetrain changed, a targeted scan costs **1 call instead of 6**. Across ~138 runs/yr that's the single largest saving available, and it needs no verification — just a CLI flag.

---

## 5. SQLite schema

Trigger-agnostic. `source_kind` is `'version'` (V) or `'microversion'` (M); `source_id` is the version ID or the element microversion ID.

```sql
-- One row per (drawing, observed state).
CREATE TABLE drawing_state (
    source_id      TEXT NOT NULL,      -- version.id (V) | element.microversionId (M)
    source_kind    TEXT NOT NULL,      -- 'version' | 'microversion'
    element_id     TEXT NOT NULL,
    document_id    TEXT NOT NULL,
    document_name  TEXT,
    element_name   TEXT,
    version_id     TEXT,               -- V only
    version_name   TEXT,               -- V only
    configuration  TEXT,
    observed_at    TEXT NOT NULL,
    settled_at     TEXT,               -- M only: for debounce
    PRIMARY KEY (element_id, source_id)
);
CREATE INDEX idx_ds_document ON drawing_state(document_id);

-- Export attempts, separate so a failure retries without losing observation.
CREATE TABLE export (
    element_id     TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    format         TEXT NOT NULL DEFAULT 'PDF',
    status         TEXT NOT NULL,      -- PENDING|ACTIVE|DONE|FAILED|SKIPPED
    translation_id TEXT,
    output_path    TEXT,
    sha256         TEXT,
    byte_size      INTEGER,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    started_at     TEXT,
    completed_at   TEXT,
    PRIMARY KEY (element_id, source_id, format),
    FOREIGN KEY (element_id, source_id) REFERENCES drawing_state(element_id, source_id)
);
CREATE INDEX idx_export_status ON export(status);

-- Per-document cursor.
CREATE TABLE sync_state (
    key        TEXT PRIMARY KEY,       -- 'last_version:{did}' | 'last_mv:{eid}'
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Stage B: publication to GitHub. Separate table so a GitHub failure never
-- touches Onshape quota on retry.
CREATE TABLE publish (
    element_id   TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    format       TEXT NOT NULL DEFAULT 'PDF',
    status       TEXT NOT NULL,     -- PENDING|COMMITTED|LINKED|NO_MATCH|AMBIGUOUS|FAILED
    identifier   TEXT,              -- extracted part number / key
    repo_path    TEXT,
    commit_sha   TEXT,
    blob_url     TEXT,
    issue_number INTEGER,
    comment_url  TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    published_at TEXT,
    PRIMARY KEY (element_id, source_id, format),
    FOREIGN KEY (element_id, source_id) REFERENCES drawing_state(element_id, source_id)
);
CREATE INDEX idx_publish_status ON publish(status);

-- Local quota accounting — the EDU pool is shared and Onshape exposes usage
-- only in company settings, never via API.
CREATE TABLE call_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status   INTEGER NOT NULL,
    counted  INTEGER NOT NULL,         -- 1 if 2xx/3xx, else 0
    at       TEXT NOT NULL
);
CREATE INDEX idx_calllog_at ON call_log(at);
```

**Crash-safety:** write the `export` row as `PENDING` *before* the POST, so an interrupted run resumes without re-spending detection calls. Cursors advance only on success. The `(element_id, source_id)` PK makes re-runs idempotent — worst case is a wasted detection call, never a duplicate PDF.

---

## 5b. Stage B — PDF → GitHub

**Decoupled from Stage A by design.** Stage A is rationed (2,500/yr, shared, fragile). Stage B is effectively free (GitHub: 5,000 req/hr authenticated) and reliable. The SQLite manifest is the seam: Stage B re-runs freely against PDFs already paid for, and a GitHub outage can never burn Onshape quota on retry.

```
Stage A: Onshape → PDF          quota-constrained
              ↓ SQLite (export.output_path, status=DONE)
Stage B: PDF → git commit → issue comment       unconstrained, resumable
```

### 5b.1 Flow

```
1. SELECT exports WHERE status='DONE' AND no publish row
2. identifier = extract(element_name)              # regex, configurable — §5b.2
3. copy PDF → <repo>/drawings/{identifier}_{versionName}.pdf
4. git add / commit / push                          → commit_sha, blob_url
5. GET /search/issues?q=repo:{org}/{repo}+{identifier}+in:title
6. comment blob_url on the matched issue            → comment_url
7. mark publish row LINKED
```

GitHub renders PDFs inline in the blob view, so the issue link previews properly — the reason repo-commit beats release assets here.

### 5b.2 ⚠️ The identifier problem

**There is no `partNumber` in this design.** That field lived on `BTRevisionInfo` and died with Release Management. `BTDocumentElementInfo` exposes only **`name`** — the drawing tab name.

Two sources:

| Source | Cost | Viable if |
|---|---|---|
| **Drawing tab name** (`element.name`) | **Free** — already in the `elements` response | Tab names contain a stable, matchable key |
| **Element metadata** (`GET /metadata/d/{did}/{wvm}/{wvmid}/e`) | **1 call per changed doc** — returns *all* elements' metadata at once, not one per drawing | You actually populate a Part Number property |

Prefer the tab name (free). Fall back to metadata only if tab names aren't matchable — at 1 call/changed doc (~200/yr) it's affordable but not free.

### 5b.2a Naming convention — RESOLVED & TESTED

Drawings carry **no part-number metadata**; the **tab name is the identifier**. Convention:

```
<team>-<YY><bot>-[A]<subsystem><part>

1250-26B-501     team 1250, 2026 B-bot, subsystem 5,  part 01
1250-26A-A503    team 1250, 2026 A-bot, subsystem 5,  subassembly drawing 03  (A prefix)
1250-26B-1101    team 1250, 2026 B-bot, subsystem 11 (chassis), part 01
```

Extraction regex (**tested** against all known examples):

```python
PATTERN = re.compile(
    r'(?P<id>(?P<team>\d{3,5})-(?P<yy>\d{2})(?P<bot>[A-Z])-(?P<asm>A?)(?P<sn>\d{3,4}))'
)
```

`.search()` (not `.match()`) so `"1250-26B-501 Gearbox Plate"` and `"Chassis 1250-26B-1101"` both resolve — tab names drift, and the ID is what matters.

**The match key is the full `id` group** (`1250-26B-501`), matched against issue titles. **No decomposition required for Stage B.**

> ⚠️ **Known ambiguity — deferred, not resolved.** Subsystem is 1–2 digits (chassis = 11) and part is assumed 2 digits, so `sn` splits as `sub=sn[:-2], part=sn[-2:]`. This makes `1101` ⇒ *subsystem 11, part 01* — but if part numbers can be **3 digits**, `1101` could equally be *subsystem 1, part 101*. The source comment describing `501` as "part 001" suggests 3-digit parts may exist.
>
> **Harmless today** (matching uses the whole string). **Blocking only if** subsystem-based grouping/filtering is added later. Resolve then by confirming: *are part numbers always exactly 2 digits?*

**Non-conforming names** (e.g. `"Drawing 3"`) yield no match ⇒ `NO_MATCH` ⇒ PDF still committed, just unlinked. See §5b.3.

**Still needed:** real issue titles to confirm the match target, and the target repo.

### 5b.3 Match policy — needs a decision

Issue search is fuzzy; the failure modes are the design:

> **Note:** non-conforming drawing names never reach Stage B at all — they're gated out in Stage 2 (§4.8). So every PDF arriving here *has* a valid identifier. The cases below are about the **issue lookup**, not the name.

| Case | Proposed handling |
|---|---|
| **1 match** | Comment the PDF link. Mark `LINKED`. |
| **0 matches** | Mark `NO_MATCH`, log, **do not create an issue**. PDF is still committed and linkable — the identifier is valid, there's just no ticket yet. |
| **2+ matches** | Mark `AMBIGUOUS`, log, comment on none. Silent wrong-issue comments are worse than none. |
| **Issue closed** | Comment anyway? Or skip? — **your call** |
| **Re-export of same drawing** | New version ⇒ new PDF ⇒ new comment on the same issue (a running history), rather than editing the old comment. |

Rationale for not auto-creating on `NO_MATCH`: at ~10 runs/week, an auto-create bug spams the tracker, and issues are cheap to open by hand but tedious to clean up.

### 5b.4 Tooling

`gh` is **not installed** (git 2.50.1 is). Options:
- **`winget install GitHub.cli`** → `gh` handles auth, issue search, and commenting in a few lines. *Recommended.*
- **REST API + PAT** from Python — no install, but you hand-roll auth and token storage. Fits the "stdlib only, students can clone and run" goal.

Either way Stage B needs a credential; `gh auth login` is the least painful.

---

## 6. Quota calendar — ✅ CORRECTED 2026-07-16

> **An earlier draft of this spec said the reset was Feb 19. That was wrong** and drove several bad conclusions. Corrected from the Developer tab's own counter.

### ⚠️ The reset date is UNRESOLVED — and it does not matter

The Developer tab shows **two mutually inconsistent fields** (observed 2026-07-16):

```
Tracking start date:      Feb 19, 2026      -> 147 days before today
Days into billing cycle:  315 / 365 (86.3%) -> cycle began ~2025-09-04, ends ~2026-09-04
```

They are **168 days apart** and cannot describe the same period. Two readings:

| Reading | Reset | Rationale |
|---|---|---|
| **Billing cycle governs** | **~Sep 4, 2026** | The limits doc's pro-ration example ties allocation to **seats** — *"adds 1 seat when they have half of their API allocation period remaining… 5,000 in their current cycle… 10,000 in the next full cycle."* Seats are billing. "Tracking start" would then be when Onshape enabled usage *recording*, mid-cycle. |
| **Tracking start governs** | **~Feb 19, 2027** | The annual allowance runs from when tracking began. |

**Neither is documented.** `auth/limits/` states no reset semantics; `help/Plans/developer.htm` defines neither field.

#### It doesn't change the design

Projected use is **~600–1,200/cycle** against **2,500**:

| Reset | Coverage | Fits? |
|---|---|---|
| Sep 4, 2026 | Fresh cycle Sep 2026 → Sep 2027 contains the whole season | ✅ ~2× headroom |
| Feb 19, 2027 | Current pool covers Jul 2026 → Feb 2027 incl. most of build season; fresh 2,500 for Feb–Apr | ✅ ~2× headroom |

The §6.2 ≥1,500-at-January tripwire holds under **both** (spend to Jan 1 is ~200–250 either way).

> 🚫 **Do NOT run a "spend it before it expires" sprint.** An earlier draft of this section claimed the current 2,500 was use-it-or-lose-it before Sep 4 and urged burning it on integration testing. **That was based on the unproven billing-cycle reading.** If tracking-start governs, that sprint drains the pool on a false deadline and leaves nothing for build season. The asymmetry is one-directional:
>
> - Assume Sep 4, actually Feb 19 → **run dry mid-season. Catastrophic.**
> - Assume Feb 19, actually Sep 4 → **conserve, get a bonus reset. Harmless.**
>
> **Plan against the later date (~Feb 19, 2027).** Costs nothing; the budget fits regardless.

**To resolve definitively:** email **api-support@onshape.com** — free, and you're a company admin. Until then, treat the reset as ~Feb 19 2027 and ignore the question.

**Safety net (from `help/Plans/developer.htm`):** *"Notification emails are sent to all admins at 25%, 50%, 75%, and 100% of usage."* You will be warned long before a wall, under either reading.

**Undocumented usage endpoint** (community-sourced, unverified): `GET /api/v13/metrics/api/summary?startDate=<ISO>` — `keyCount` = personal-key requests; add `ownerId` for company-key requests. Callable **free from the Explorer** (session auth). May be useful for `call_log` reconciliation.

### 6.1 Projected spend, per cycle

Basis: **user-initiated**, ~10 runs/week × ~12-week season = **120 runs**, plus ~18 off-season = **~138 runs/cycle**. 6 documents. ~60 drawing exports. All within the Sep→Sep cycle.

**Measured from the first live run (2026-07-16):** a full export of one drawing cost **6 calls** (versions 1, elements 1, translate 1, poll 2, download 1). A no-op run costs **1 call/doc** (versions only).

| | Scan all 5 (`py export.py`) | Subset (`--doc chassis`) |
|---|---|---|
| Stage 1 versions | 138 × 5 = **690** | 138 × 1 = **138** |
| Stage 2 elements (~1.5 changed docs/run) | 207 | ~138 |
| Stage 3 exports (60 × ~5–6) | 330 | 330 |
| Development / integration | 50 | 50 |
| **Total** | **≈1,277** | **≈656** |
| **% of 2,500 — _shared company pool_** | **~51%** | **~26%** |

> ⚠️ **Educator = 2,500 per _company_, not per user.** This is the whole team's pool. At ~51% for a full-scan habit this tool becomes the dominant consumer — survivable, but thin. **Subset scanning halves it.**

**Stage 1 dominates** — 690 calls to ask "anything new?" 138 times. Levers, in order of value:

1. **Subset scanning (§4.9) — now the ONLY lever that matters.** `--doc chassis` costs **1 call** vs 5. Since runs are user-initiated you usually know what changed. Worth ≈550 calls/cycle — more than everything else combined.
2. **Run less than 10×/week off-season** — the season number is the season's; weekly is plenty in July.
3. ~~Stage 0 gate~~ — **disabled** (§4.3): the folder holds ~40–60 docs, so it costs 3 calls, not 1, and makes lever 1 four times worse.

Habitual subset scanning lands near **~650/cycle (~26%)**, which is comfortable.

**The season fits under either reset reading (§6)** — ~600–1,200 projected against 2,500, roughly 2× headroom in both cases. This is why the unresolved reset date is immaterial: no design decision turns on it.

### Guardrails

1. **Don't poll aggressively.** Daily during season is plenty. Hourly ⇒ ~6,500/yr ⇒ **over quota**. Frequency is the whole ballgame.
2. **Poll translations with backoff** (start ~5s, exponential). Each *successful* poll bills.
3. **Track spend locally** (`call_log`) — Onshape gives no API-side usage number.
4. **Hard budget cap** in config; refuse to run past N calls/year. A runaway loop shouldn't cost the team its season.
5. **Reserve ≥1,500 calls going into 1 Jan 2027.** Holds under **both** reset readings (§6) — spend to Jan 1 is ~200–250 either way. A tripwire, not a squeeze.
6. **Never spend against an assumed reset date.** The reset is unresolved (§6); plan against the **later** one (~Feb 19 2027). Conserving is free; a "burn it before it expires" sprint on the wrong date drains the season.
6. **Webhooks are quota-exempt** (`onshape.model.lifecycle.createversion`) and would cut detection to ~0 — but need a public HTTPS endpoint. **v2 only.**

---

## 7. Development strategy — spend ≈0 quota

Verbatim from the [limits doc](https://onshape-public.github.io/docs/auth/limits/):

> **Exempt:** *"Calls made from the Onshape browser, mobile clients, or the Onshape API Explorer **(when authenticated via an Onshape session)**"*
> **Counted:** *"Calls made from the Onshape API Explorer **when authenticated via API keys or OAuth2**"*

**The Glassworks API Explorer is free when session-authenticated.** Sign into Onshape in another tab and let it pass the session through.

> ⚠️ **Do NOT click *Authorize* and paste API keys into the Explorer.** That flips the identical call from free to billable — the easiest way to burn the season's quota by accident.

| Phase | Tool | Cost |
|---|---|---|
| 1. Settle §4.1; capture response shapes | **Explorer (session auth)** | **0** |
| 2. Copy real JSON → `fixtures/*.json` | Explorer + clipboard | **0** |
| 3. Build & test exporter against fixtures | Local, offline | **0** |
| 4. Test error paths (401/404/429) | Real calls — **4xx/5xx exempt** | **0** |
| 5. Final integration test | Real API-key calls | ~20–50 |
| 6. Production | Real API-key calls | ~240–680/yr |

Record the fixtures. They pay for themselves — the exporter can then be refactored and regression-tested indefinitely at zero cost, which matters when the pool doesn't refill until Feb 19.

**Extra dev pool:** EDU Student/Free is **2,500 per *User***, not per company. A student's personal free account carries its own independent 2,500; developing against a personal-account copy never touches the team pool. Now fully usable — with Plan A dead, nothing in this design needs company features.

---

## 8. Open questions

| # | Question | Blocking? |
|---|---|---|
| 8.0 | ✅ **Resolved** — drawings are `APPLICATION`/`onshape-app/drawing`, not `DRAWING` (§4.6). Stage 2 validated offline against real fixtures (§8.0a). | — |
| 8.1 | ✅ **Resolved** — 5 documents, in `config.json`: chassis(1), hopper(2), intake(3), indexer(4), shooter(5). Document `N00 - Name` ⇒ subsystem `N`, matching the drawing identifiers inside it (free misfile check, §4.8). **Top-level robot doc `77ccfe85426b6dca10c64431` deliberately EXCLUDED** — no drawings expected, and it would burn ~138–276 calls/cycle: it references all 5 subsystems, so its `modifiedAt` moves on nearly every run, clearing Stage 0 and spending a Stage 1 call only to hit `purpose!=0` auto-versions or 0 drawings. Add it only if drawings appear there. | — |
| 8.2 | ✅ **Resolved** — nothing else on the team touches the API. The full 2,500 is available to this tool. Revisit if that changes. | — |
| 8.3 | ✅ **Resolved** — all docs under `parentId = 2b21f048c6c582e58dd11553`. Stage 0 enabled. | — |
| 8.4 | ✅ **Resolved** — `modifiedAt` bumps on version creation (§4.3); `versions[-1]` is newest (§4.4). | — |
| 8.5 | **Which field governs the quota reset?** Developer tab shows `Tracking start date: Feb 19, 2026` **and** `Days into billing cycle: 315/365` — 168 days apart, mutually inconsistent (§6). Undocumented. **Ask api-support@onshape.com.** Meanwhile assume the later date (~Feb 19 2027); the budget fits either way, so nothing is blocked. | Nice-to-have |
| 8.6 | Output naming. Proposal: `{identifier}_{versionName}.pdf` (identifier from §4.8's regex, never the raw name), configurable root; configured drawings need a suffix. | Cosmetic |
| 8.6 | Where do PDFs land — local, OneDrive, repo? | Non-structural |
| 8.7 | Should a run ever export a drawing that changed **but wasn't versioned**? (i.e. workspace WIP) Current design: **no** — versions only. | Confirm |

### 8.0a Empty drawings — ✅ RESOLVED

`elementType=DRAWING` returned `[]` on a document containing three drawings. **Cause: wrong constant** (§4.6). Scope stands — drawings do live in the subsystem documents.

**Stage 2 validated end-to-end against `response_1-5.json`** (`fixtures/`, replayed offline, 0 API calls):

```
8 elements total
  -> elementType=APPLICATION + dataType=onshape-app/drawing  : 3 drawings
  -> deleted filter (via .get, field absent)                 : 3 remain
  -> ID regex gate (§4.8)                                    : 0 export, 3 SKIPPED
       - 'Tube 2"x1"x18.5" Drawing 1'   mv=46e3d634...
       - 'Tube 2"x1"x19.5" Drawing 1'   mv=1b6c0b5d...
       - 'Tube 1"x1"x27" Drawing 1'     mv=209d7039...
```

**Correct behaviour today:** the chassis exports nothing, because no drawing yet follows the naming convention. The run must *say so loudly* (§4.7) — this is precisely the state that must never be confused with "nothing changed".

**No guessed constants remain in Stage A.** Every endpoint, parameter, filter value, and field is confirmed against live v16 or a real response.

---

**Resolved:** trigger (§4 — version + microversion hybrid); tier (Educator, 2,500/company shared); frequency (user-initiated, ~10×/wk in season); runtime (§10); Stage 0 (§4.3 — proven); version ordering (§4.4); free filters (§4.5); folder ID; naming convention (chassis = subsystem **1**; `100` parses correctly).

---

## 9. Failure modes

| Mode | Handling |
|---|---|
| `402` quota exhausted | **Halt and alert loudly.** Never retry. On a shared EDU pool this is a team-wide event. |
| `429` rate limited | Honour `Retry-After`. Free (4xx uncounted). |
| Translation `FAILED` | Record `last_error`; bounded retries; don't block the run. |
| Drawing not translatable | Mark `SKIPPED`; don't retry each run. |
| Interrupted mid-run | `PENDING`-before-POST + cursor-on-success ⇒ resumable. |
| Local budget cap hit | Stop, report. Prevents a bug burning the season. |
| **Drawing renamed to a conforming name** (the imminent case — all 3 chassis drawings) | Previously skipped ⇒ **no export record exists** ⇒ `last_exported_mv.get(eid)` is `None` ⇒ `microversionId != None` ⇒ **exports on the next version.** ✅ Correct by construction — regardless of whether a rename moves `microversionId`. ⚠️ Requires the lookup to **default**, not subscript: `last_exported_mv[eid]` would `KeyError` on every first-time drawing (same class of bug as `deleted`, §4.7). |
| Renamed but no new version created | Stage 1 skips the doc ⇒ no export until someone versions. **Expected** — versions gate everything by design. |
| Two drawings resolve to the same identifier | Last-write-wins on the PDF path. Log a collision; **never** silently overwrite. |

---

## 10. Runtime — resolved

**Python 3.13.5** at `C:\Users\moorh\AppData\Local\Programs\Python\Python313`.

- Invoke as **`py`**, not `python` (§12).
- `sqlite3` 3.49.1 — stdlib, no dependency.
- **`requests` NOT installed** → `py -m pip install requests`, or use stdlib `urllib.request` and keep the project dependency-free (nicer for a team repo students clone and run). **Recommend stdlib.**
- Scheduling: Task Scheduler against this python.org install works cleanly (a Store-installed Python would not — §12).

> ⚠️ **Console encoding: the Windows console is `cp1252`, not UTF-8.** Printing `→` or `…` raises `UnicodeEncodeError` and **crashes the run** — observed while validating §8.0a. For a tool students clone and run, that's unacceptable. Either keep console output **strictly ASCII**, or set `PYTHONIOENCODING=utf-8` / `sys.stdout.reconfigure(encoding="utf-8")` at startup. Note this affects *console* output only — file I/O must still specify `encoding="utf-8"` explicitly, since `cp1252` is also the default there and drawing names contain characters like `"` that survive but non-ASCII ones would not.

---

## 11. Out of scope (v1)

- Webhook receiver (§6.6) — revisit if frequency demands.
- DWG/DXF export (`formatName` swap; trivial).
- Non-drawing element types.
- Backfill of historical versions (one-time; budget separately).

---

## 12. Appendix: Windows Store stub vs. real Python

The `python` command hits a **Microsoft App Execution Alias** — a 0-byte reparse point at
`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` → `AppInstallerPythonRedirector.exe`.
It is **not Python**. Its only job is to open the Microsoft Store when Python is missing. Windows ships these aliases enabled so `python` prints a Store ad instead of "command not found."

**Why it shadows the real install** — PATH order:

```
 30: ...\Programs\Python\Launcher        ← py.exe lives here   ✓
 31: ...\Microsoft\WindowsApps           ← the redirector stub  ✗
     ...\Programs\Python\Python313       ← NOT ON PATH AT ALL
```

The python.org installer added the **Launcher** but not `Python313\` ("Add Python to PATH" left unchecked). So `python` falls through to the stub at #31 while **`py` works** from #30. Nothing is broken — the command name is just wrong.

**Fixes** (any one): use **`py`** *(recommended)*; disable the aliases via *Settings → Apps → Advanced app settings → App execution aliases*; or put `Python313\` on PATH above WindowsApps.

**Store Python vs python.org**, had you installed the other one:

| | Store Python | python.org (yours) |
|---|---|---|
| Install | MSIX sandboxed container | Normal per-user install |
| File & registry writes | **Redirected** to `AppData\Local\Packages\…\LocalCache` | Direct |
| Writing to install dir | Blocked | Allowed |
| Registry keys | Virtualized — IDEs struggle | Proper |
| **Task Scheduler / service** | **Breaks** under SYSTEM/other users | **Works** |
| C-extension packages | Occasional breakage | Fine |

**Bottom line:** the stub is a signpost, not an interpreter — and you already have the better of the two real options. For a *scheduled* exporter that last row is decisive: the Store build's Task Scheduler behaviour would have bitten you the moment you automated this.

---

## Sources

- [API Limits](https://onshape-public.github.io/docs/auth/limits/) — quotas, 402/429, counted vs exempt
- [Import & Export](https://onshape-public.github.io/docs/api-adv/translation/) · [Drawings API](https://onshape-public.github.io/docs/api-adv/drawings/)
- [API Keys](https://onshape-public.github.io/docs/auth/apikeys/) · [Webhooks](https://onshape-public.github.io/docs/app-dev/webhook/)
- [Glassworks API Explorer](https://onshape-public.github.io/docs/api-intro/explorer/) — session vs key auth
- **[Live OpenAPI spec](https://cad.onshape.com/api/v16/openapi)** — v16, version `1.217.82698`. **Authoritative** for every endpoint/param/schema claim above. Also served at `/api/openapi`. Re-download to re-verify; it's ~1.9 MB and free (not an authenticated API call).
- ⚠️ [onshape-clients/openapi.json](https://github.com/onshape-public/onshape-clients/blob/master/openapi.json) — **STALE: last commit 2021-11-15, spec version 1.113.** An earlier draft of this spec was built on it. **Do not use.** Listed only to warn it off.
