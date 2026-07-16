# Fixture Capture Protocol

**Cost: 0 API calls.** All reads happen in the Glassworks Explorer under **session auth**, which is quota-exempt.

## Setup

1. Sign into Onshape in one browser tab.
2. Open **https://cad.onshape.com/glassworks/explorer/** in another.
3. **DO NOT click *Authorize*.** Leave it unauthenticated — it inherits your session.
   Authorizing with API keys flips every call from **free → billable**. This is the only way to get this wrong.

Chassis document (subsystem 11):
```
did = 9277e520a8289c72778da2ae
wid = 7b7c1c4a728bdb6e94ce23eb
```

Save each full response into this folder under the given filename (or paste into chat).
**Full JSON, not excerpts** — the point is to catch fields we don't know to look for.

---

## Phase 1 — BEFORE (read-only, 4 calls)

| # | Operation | Params | Save as |
|---|---|---|---|
| 1 | `Documents → getDocument` | `did` | `01-document-before.json` |
| 2 | `Documents → getDocumentVersions` | `did` | `02-versions-before.json` |
| 3 | `Documents → getElementsInDocument` | `did`, `wvm=v`, `wvmid=`**newest version id from #2**, `elementType=DRAWING` | `03-elements-at-version.json` |
| 4 | `Documents → getElementsInDocument` | `did`, `wvm=w`, `wvmid=7b7c1c4a728bdb6e94ce23eb`, `elementType=DRAWING` | `04-elements-at-workspace.json` |

> If #3 returns an empty list, the chassis may have no drawings at that version — try an older version, or a document you know has drawings. Say so and we'll adjust.

---

## Phase 2 — THE ONE WRITE ⚠️

**In Onshape (not the Explorer): create a version on the chassis document.** Change nothing else.

- Suggested name: `API-TEST-2026-07-16` — obvious to teammates, easy to ignore.
- **This is a permanent, team-visible write** to the document your top-level assembly references. Your call.
- **Lower-impact alternative:** do this whole experiment on a throwaway scratch document instead. `modifiedAt` semantics are identical. You'd lose the real drawing names from #3/#4 — so capture those from the chassis and do Phase 2/3 in the scratch doc.

---

## Phase 3 — AFTER (2 calls)

| # | Operation | Params | Save as |
|---|---|---|---|
| 5 | `Documents → getDocument` | `did` | `05-document-after.json` |
| 6 | `Documents → getDocumentVersions` | `did` | `06-versions-after.json` |

---

## What each pair decides

| Comparison | Question | Consequence |
|---|---|---|
| **01 vs 05** — `modifiedAt` | Does creating a version bump the document's `modifiedAt`? | **Gates Stage 0** (~400 calls/yr). If NO ⇒ drop Stage 0 permanently; it would silently skip new versions. |
| **02 vs 06** — where's the new version? | Is `versions[0]` or `versions[-1]` newest? | No sort param exists (§4.4). Decides `newest = ?` or forces client-side `createdAt` sort. |
| **02 / 06** — `purpose` values | `purpose` is an **undocumented int32**. What values do your versions carry? | May distinguish version *kinds*. Could be a free filter — or meaningless. Unknown until seen. |
| **02 / 06** — `parent` | Versions form a chain. Linear or branched? | If your team branches, `versions[-1]` may not mean what we assume. |
| **03** — drawing tab names | Do real names match the tested regex (`1250-26B-501`)? | **Gates Stage B matching.** If names are `"Drawing 3"`, fall back to element metadata (~200 calls/yr). |
| **03** — `microversionId` | Present and distinct per drawing? | **The core change-detection filter** (§4.1). |
| **03 vs 04** — version vs workspace | Do the same drawings show different `microversionId`s at version vs workspace? | Confirms microversion tracks content, not just tab identity. |
| **01 / 05** — `hasReleaseRevisionableObjects` | Does it corroborate "no Release Management"? | Cross-check on the Plan A/B decision. |

---

## Notes

- Everything above is **read-only except Phase 2**.
- Any `4xx`/`5xx` is **quota-exempt** — a wrong parameter costs nothing. Experiment freely.
- Paths in the Explorer have **no `/api` prefix** and `elementType` is a **string** (`DRAWING`), per live v16 (`1.217.82698`).
