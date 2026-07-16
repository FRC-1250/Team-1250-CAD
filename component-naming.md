[Home](README.md)

This document details the Team 1250 CAD naming conventions for components, drawings, and files.

---

# Part numbers — `1250-26B` forward

**Applies from `1250-26B` onward.** Components created before this use the [legacy scheme](#legacy-scheme--before-1250-26b).

Parts and drawings follow the pattern:

`<TEAM>-<YY><BOT>-[A]<SUBSYSTEM><PART>`

...where:
- `<TEAM>` is the team number: `1250`
- `<YY>` is the last two digits of the competition year (example: 2026 -> `26`)
- `<BOT>` is the robot letter, `A` or `B` (matching the *Rebuilt* / *Rebuilt-B* projects)
- `A` — an optional prefix on the number marking a **subassembly** drawing rather than a part
- `<SUBSYSTEM>` is the subsystem number, matching its Onshape document:
  - `1` — Chassis
  - `2` — Hopper
  - `3` — Intake
  - `4` — Indexer
  - `5` — Shooter
- `<PART>` is a two-digit part number within that subsystem
- A trailing description is allowed and encouraged: `1250-26B-101 Gearbox Plate`

Examples:

| Name | Meaning |
|---|---|
| `1250-26B-101` | B-bot, subsystem 1 (Chassis), part 01 |
| `1250-26B-502` | B-bot, subsystem 5 (Shooter), part 02 |
| `1250-26A-A503` | A-bot, Shooter, **subassembly** drawing 03 |

## Drawings

**Onshape drawing tabs must be named with the part number.**

The automated PDF exporter (`tools/drawing-export/`) **only exports drawings whose
tab name matches this pattern.** This is deliberate — it keeps the numbering honest.
A drawing named `Tube 2"x1"x18.5" Drawing 1` is skipped and reported, not exported.

Exported PDFs land in this repo under `<TEAM>-<YY><BOT>/`, for example
`1250-26B/1250-26B-101.pdf`. A drawing is only exported once its document has a new
**version** — saving the workspace is not enough.

## Issues

Each part should have a GitHub Issue tracking it. **Put the part number in the issue
title or description** so the exporter can link the drawing's PDF to it. Issues with
no part number cannot be matched automatically.

---

# Legacy scheme — before `1250-26B`

Earlier components use the GitHub Issue number as their identifier:

`FRC1250-<YY>-<####> <SOMETHING-DESCRIPTIVE>`

- `<YY>` is the last two digits of the competition year
- `<####>` is the zero-padded **GitHub Issue number** that tracks the part
- e.g. issue #91 -> `FRC1250-26-0091 Chassis Tubes`

> **Note:** an earlier version of this document specified a `<SUBSYSTEM>` code
> (`DRIVE`, `ARM`, `SHOOTER`, `INTAKE`, `CLIMB`) between the year and the number.
> **It was never used** — none of the 91 issues created under this scheme include
> one. The pattern above reflects what is actually in the repo.

---

# Where parts should live

- Parts require `CAM` (computer aided manufacturing) tool paths that live within the
  Fusion files. These can pose processing challenges when part of a large assembly.
  Therefore, parts that will be CNC machined should always be external linked parts,
  not local parts within an assembly.
- 3D printed parts are easy to export, so they may be kept local (probably easiest)
  or may be external.
- Parts made by people require a drawing to be created, but do not pose processing
  issues, so they may be kept local (probably easiest) or may be external.

---

# Why go to all of this trouble?

- The **year** field allows us to track parts over time.
- The **bot** field separates the two robots.
- The **subsystem** field helps everyone understand the basic context, and ties the
  part to its Onshape document.
- The **part** field makes it unique.
- The **descriptive** field makes it friendly for humans, because the numbers can be
  hard to remember.
