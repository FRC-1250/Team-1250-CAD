[Home](README.md)

This document details the Team 1250 CAD naming comventions for components and files.

# Team Components

Custom components created by the team should follow the pattern:

`FRC1250-<YY>-<SUBSYSTEM>-<###>-<TYPE>-<SOMETHING-DESCRIPTIVE>`
...where:
- `<YY>` is the last two didgits of the competition year (example: 2026 -> `26`)
- `<SUBSYSTEM>` is the code name for the subsystem:
  - `DRIVE`
  - `ARM`
  - `SHOOTER`
  - `INTAKE`
  - `CLIMB`
  - etc...
- `<###>` a unique, mostly sequential 3-digit number for the specific parts in the subsystem
- `<TYPE>` is the code name for how the component is made:
  - `FDM` for 3D printed parts (Fused Deposition Modeling)
  - `CNC` for parts cut on a CNC machine
  - `DWG` for parts that are cut by humans from a drawing... usually stock cut to legth, maybe including a few extra holes
- `<SOMETHING-DESCRIPTIVE>` some text that describes the part, because the numbers can be hard to remember

Why go to all of this trouble?
- The year field allows us to track parts over time.
- The subsystem field helps everyone understand the basic context.
- The number field makes it unique.
- The type field allows us to filter and sort by how parts are fabricated.
- The something descriptive field makes it friendly for humans.

---
A note on where parts should live:
- `CNC` parts require `CAM` (computer aided manufacturing) tool paths that live within the Fusion files.  These can pose processing challenges when part of a large assebly. Therefore, `CNC` parts should always be external linked parts, not local parts within an assembly.
- `FDM` parts are easy to export, so they may be kept local (probably easiest) or may be external.
- `DWG` parts require a drawing to be created, but do not pose processing issues, so they may be kept local (probably easiest) or may be external.

