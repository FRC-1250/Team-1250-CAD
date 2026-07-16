# Drawing Export

Exports Onshape drawings to PDF when they change, commits them to this repo, and
links them to the GitHub Issue that tracks the part.

```
Stage A   Onshape -> PDF      export.py    (quota-constrained -- read below)
             |  state.db
Stage B   PDF -> GitHub       publish.py   (free, retryable)
```

## 🚨 Read this before running anything

**We get 2,500 Onshape API calls per YEAR, shared by the entire team.** Not per
person, not per month. Running out returns `402` and everything stops until the
cycle resets.

- **A no-op run costs 1 call per document.** A full export of one drawing costs ~6.
- **Use `--doc <key>` whenever you know what changed.** It's 1 call instead of 5,
  and it's the single biggest lever we have.
- **Never poll on a timer.** Runs are manual, on purpose. Hourly would blow the
  yearly budget by March.
- **Never explore the API with a key.** Use the [Glassworks Explorer](https://cad.onshape.com/glassworks/explorer/)
  signed in through your browser — it's **free**, but only if you *don't* click
  *Authorize*. Clicking it makes every call billable.

Full reasoning: [`docs/drawing-export-spec.md`](docs/drawing-export-spec.md) §6.
Traps and hard-won facts: [`docs/CLAUDE.md`](docs/CLAUDE.md).

## Setup

```bash
cp secrets.example.json secrets.json     # gitignored -- never commit it
# fill in Onshape access_key / secret_key (My Account -> Developer)
# and github_token (repo scope) for Stage B
```

Needs Python 3. On Windows use **`py`**, not `python` (`python` hits a Microsoft
Store stub that isn't an interpreter). No pip installs — stdlib only.

## Usage

```bash
py export.py --doc chassis        # scan one document      ~1 call if unchanged
py export.py                      # scan all five          ~5 calls if unchanged
py export.py --dry-run            # refuse all live calls

py publish.py --dry-run           # show what would be committed/linked
py publish.py                     # commit PDFs + comment on matched issues
py publish.py --push              # ...and push
py publish.py --create-issues     # ALSO open issues for unmatched parts (see below)

py tests/test_replay.py           # full offline test suite, 0 API calls
```

## What gets exported

A drawing is exported only when **all** of these hold:

1. Its document has a **new version** — saving the workspace is not enough.
2. That version isn't an Onshape auto-version, and actually changed something.
3. The drawing itself changed since its last export.
4. **Its tab name matches the part-number convention** — see
   [`component-naming.md`](../../component-naming.md).

Rule 4 is a deliberate gate: `Tube 2"x1"x18.5" Drawing 1` is **skipped**, and the
run says so. If a drawing you expect isn't showing up, check the skip list first —
it's almost always the name.

PDFs land in `<repo>/1250-26B/1250-26B-101.pdf`. No version suffix: git history
*is* the version history, and issue links are pinned to a commit.

## Issue linking

`publish.py` searches all issues (open and closed) for the part number and comments
the PDF link on the match.

**`--create-issues` is off by default.** As of 2026-07-16 no issue contains a
`1250-26B`-style part number, so nothing matches yet and every part looks "new."
Enabling it blind would open one issue per drawing, some duplicating issues that
already exist (#91 *Chassis Tubes* already covers the chassis tube drawings). Run
without it, read the plan, then decide.

## Fixtures

`fixtures/` holds real Onshape API responses, captured free via the Explorer
(see [`fixtures/CAPTURE.md`](fixtures/CAPTURE.md)). The test suite replays them, so
the whole pipeline is testable offline at zero cost.

They are not a nicety. They caught two bugs the official spec would have waved
through — `deleted` is documented but never returned; `elementType=DRAWING` matches
no drawing that exists. **When the spec and a fixture disagree, believe the fixture.**

Personal data is redacted; structure is untouched.
