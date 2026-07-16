# Drawing Export

Exports Onshape drawings to PDF when they change, commits them here, links them to
the GitHub Issue tracking each part, and files the part on the season's project board.

```
Stage A   Onshape -> PDF        export.py     (quota-constrained -- read below)
             |  state.db
Stage B   PDF -> GitHub         publish.py    (free, retryable)
```

---

## 🚨 Read this before running anything

**We get 2,500 Onshape API calls per YEAR, shared by the whole team.** Not per
person, not per month. Running out returns `402` and everything stops until the
cycle resets.

| Rule | Why |
|---|---|
| **Use `--doc <key>` when you know what changed.** | 1 call instead of 5. Biggest lever we have. |
| **Never poll on a timer.** Runs are manual, on purpose. | Hourly would blow the year's budget by March. |
| **Never explore the API with a key.** Use the [Glassworks Explorer](https://cad.onshape.com/glassworks/explorer/) signed in via browser — it's **free**, but only if you *don't* click **Authorize**. | Clicking Authorize makes every call billable. |
| **On `402`: stop and tell someone. Never retry.** | Retrying can't help and hides a team-wide event. |

Measured: a no-op run costs **1 call per document**; a full export of one drawing
costs **~6**. Everything on the GitHub side is free.

Full reasoning: [`docs/drawing-export-spec.md`](docs/drawing-export-spec.md) §6.
Traps and hard-won facts: [`docs/CLAUDE.md`](docs/CLAUDE.md).

---

## Setup

```bash
cp secrets.example.json secrets.json     # gitignored -- never commit it
```
Fill in Onshape `access_key`/`secret_key` (My Account → Developer) and a
`github_token` (repo scope) for Stage B.

Needs Python 3. On Windows use **`py`**, not `python` — `python` hits a Microsoft
Store stub that isn't an interpreter. **No pip installs**: stdlib only.

---

## The scripts

| Script | What it does | Onshape cost |
|---|---|---|
| [`export.py`](export.py) | **Stage A.** Finds changed drawings, exports PDFs. | ~1/doc + ~6/drawing |
| [`publish.py`](publish.py) | **Stage B.** Commits PDFs, comments on issues, files on the board. | **0** |
| [`next_number.py`](next_number.py) | Next free part number per subsystem. | **0** |
| [`reconcile_users.py`](reconcile_users.py) | Maps Onshape users → GitHub logins. | 1 (or 0 with `--from-fixture`) |
| [`project_setup.py`](project_setup.py) | Per-season board setup: subsystem options + fields. | **0** |
| [`tests/test_replay.py`](tests/test_replay.py) | Full offline suite against real captured responses. | **0** |

```bash
py export.py --doc chassis        # scan one document
py export.py                      # scan all five
py export.py --dry-run            # refuse all live calls

py publish.py --dry-run           # show what would be committed/linked
py publish.py --push              # commit, push, comment, file on board
py publish.py --create-issues     # ALSO open issues for unmatched parts (see below)

py next_number.py                 # next free number in every subsystem
py project_setup.py --project N --fields      # add columns (SAFE)
py project_setup.py --project N --subsystems  # replace options (DESTRUCTIVE)
py tests/test_replay.py           # 32 assertions, 0 API calls
```

---

## The workflow

```
1. py next_number.py                    -> 1250-26B-103
2. open an issue "1250-26B-103 Gearbox Plate"   <- claims the number, assigns the work
   (a GitHub Action checks the number is valid and unused)
3. do the work in Onshape; name the drawing / derived part-studio tab 1250-26B-103
4. create a VERSION of the document
5. py export.py --doc chassis && py publish.py --push
```

**Opening the issue is what claims a number.** The old scheme used the issue number
*as* the part number, so claiming one was automatic — it just couldn't group by
subsystem. The new scheme groups, but nobody hands you a number, so step 1 exists.

Step 4 matters: **a drawing only exports once its document has a new version.**
Saving the workspace is not enough.

---

## What gets exported

A drawing is exported only when **all** of these hold:

1. Its document has a **new version** (not just a save).
2. That version isn't an Onshape auto-version and actually changed something.
3. The drawing itself changed since its last export.
4. **Its tab name matches the part-number convention** — see
   [`component-naming.md`](../../component-naming.md).

Rule 4 is a deliberate gate: `Tube 2"x1"x18.5" Drawing 1` is **skipped**, and the
run says so. **If a drawing you expect isn't appearing, read the skip list first** —
it is almost always the name.

Part-numbered **Part Studio** tabs are tracked too (the derived, detailed ones).
They get an issue and a component link but no PDF, since no drawing exists.
Unnumbered part studios are ignored silently — the source model everything derives
from isn't supposed to have a number.

---

## What the board gets filled in

| Column | Filled by |
|---|---|
| `Link to PDF` | **auto** — commit-pinned PDF |
| `Link to Drawing` | **auto** — Onshape drawing tab at that version |
| `Link to Component` | **auto** — subsystem assembly (or the part studio) |
| `Subsystem` | **auto** — from the part number |
| `Description` | **auto** — the name minus the part number |
| `Component Type` | **auto for drawings**: `DWG` (part) / `ASM` (subassembly, the `A` prefix) |
| `Component Type` | **human** for part studios — `FDM` vs `CNC` |
| `Need` / `Produced` / `Status` | **human** |

We deliberately don't guess the last three. **31 FDM parts last season had no
drawing at all**, so "a drawing exists" doesn't imply a fabrication method. A wrong
value on the board people build from is worse than a blank someone fills in.

---

## Decisions worth knowing

- **A new repo + project each season.** Avoids destructively rewriting a populated
  board's options. `project_setup.py --project N --apply` bootstraps a fresh one.
- **Stage 0 is off.** A folder `modifiedAt` gate works, but the CAD folder holds
  ~40–60 documents, so it costs 3 paginated calls, not 1 — a net loss, and it made
  `--doc` scanning 4× worse. Re-enable only if the subsystem docs get their own
  small folder. (spec §4.3)
- **The two stages are decoupled** through `state.db`. A GitHub failure can never
  burn Onshape quota on retry, and Stage B re-runs freely against PDFs already
  paid for.
- **`--create-issues` is off by default.** Creating issues is outward-facing and
  awkward to undo. Run without it, read the plan, then decide.
- **"Drawn by" is really "who versioned it."** `version.creator` is free; the
  element carries no author. See spec §10a.

---

## Fixtures

`fixtures/` holds real Onshape API responses, captured free via the Explorer
(see [`fixtures/CAPTURE.md`](fixtures/CAPTURE.md)). The test suite replays them, so
the whole pipeline is testable offline at zero cost.

They are not a nicety. They caught bugs the official spec would have waved through:

- `elementType=DRAWING` matches **no drawing that exists** (they're `APPLICATION`)
- `deleted` is documented but never returned
- `defaultWorkspace.microversion` is frozen at the workspace's creation

**When the spec and a fixture disagree, believe the fixture.** Personal data is
redacted; structure is untouched.
