"""Fixture replay: exercises the full cascade offline. ZERO API calls.

Fixtures are real captured responses (see fixtures/CAPTURE.md). They are ground
truth in a way the v16 spec is not -- they already caught two bugs the spec
would have waved through (`deleted` absent from responses; elementType=DRAWING
returning []).

    py tests/test_replay.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import identity
import osapi
import store
import export

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


class FakeClient:
    """Serves captured responses. Any unstubbed call is a test failure."""

    def __init__(self, versions, drawings):
        self._versions, self._drawings = versions, drawings
        self.calls_this_run = 0
        self.calls = []

    def folder_documents(self, parent_id, limit=50):
        raise AssertionError("stage0 should not be called in these tests")

    def versions(self, did):
        self.calls.append(("versions", did))
        return self._versions

    def elements_at_version(self, did, vid):
        self.calls.append(("elements", did, vid))
        return self._drawings

    def start_pdf_export(self, *a):
        raise AssertionError("must not export: every chassis drawing fails the regex")


def drawings_from_1_5():
    """Drawings only, via the real classifier."""
    return [e for e in fixture("response_1-5.json")
            if osapi.classify(e) == osapi.KIND_DRAWING and not e.get("deleted", False)]


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("  {}  {}{}".format("PASS" if cond else "FAIL", name, (" -- " + detail) if detail else ""))


def main():
    cfg = json.load(open(os.path.join(os.path.dirname(FIX), "config.json"), encoding="utf-8"))
    chassis = next(d for d in cfg["documents"] if d["key"] == "chassis")

    print("\n[1] element filter (spec 4.6)")
    els = fixture("response_1-5.json")
    dwgs = drawings_from_1_5()
    check("8 elements in chassis", len(els) == 8, "got {}".format(len(els)))
    check("filter isolates exactly 3 drawings", len(dwgs) == 3, "got {}".format(len(dwgs)))
    check("BOM tabs excluded", all(d["dataType"] == osapi.DRAWING_DATA_TYPE for d in dwgs))
    check(
        "elementType=DRAWING would have matched nothing",
        not [e for e in els if e.get("elementType") == "DRAWING"],
        "this is why fixtures beat the spec",
    )
    check("`deleted` absent from real responses", "deleted" not in dwgs[0],
          "e['deleted'] would KeyError; .get() required")

    print("\n[2] identifier regex (spec 4.8)")
    for e in dwgs:
        check("rejects {!r}".format(e["name"][:28]), identity.parse(e["name"]) is None)
    for name, exp_id, exp_sub, exp_folder in [
        ("1250-26B-101", "1250-26B-101", 1, "1250-26B"),
        ("1250-26B-501 Gearbox Plate", "1250-26B-501", 5, "1250-26B"),
        ("1250-26A-A503", "1250-26A-A503", 5, "1250-26A"),
        ("Chassis 1250-26B-1101", "1250-26B-1101", 11, "1250-26B"),
    ]:
        i = identity.parse(name)
        check("accepts {!r}".format(name), i and i.id == exp_id and i.subsystem == exp_sub
              and i.bot_folder == exp_folder,
              "" if i else "no match")
    i = identity.parse("1250-26A-A503")
    check("subassembly flagged", i.is_subassembly)
    check("filename has no illegal chars",
          identity.parse("1250-26B-501").filename == "1250-26B-501.pdf")

    print("\n[3] version gates (spec 4.4, 4.5)")
    vs = fixture("response_1-2.json")
    check("versions ascend; [-1] is newest",
          vs[0]["name"] == "Start" and vs[-1]["name"] == "V17")
    autos = [v for v in vs if v.get("purpose", 0) != 0]
    check("2 auto-versions detected (V4, V13)",
          [v["name"] for v in autos] == ["V4", "V13"],
          str([v["name"] for v in autos]))
    check("V17 is a no-op (microversion == V16's)",
          vs[-1]["microversion"] == vs[-2]["microversion"])

    print("\n[4] modifiedAt proves version creation bumps it (spec 4.3)")
    before, after = fixture("response_1-1.json"), fixture("response_3-5.json")
    check("document.modifiedAt moved", before["modifiedAt"] != after["modifiedAt"],
          "{} -> {}".format(before["modifiedAt"], after["modifiedAt"]))
    check("workspace.modifiedAt did NOT move (control)",
          before["defaultWorkspace"]["modifiedAt"] == after["defaultWorkspace"]["modifiedAt"])
    check("defaultWorkspace.microversion is frozen at creation (the trap)",
          before["defaultWorkspace"]["microversion"] == vs[0]["microversion"],
          "equals 'Start' from 2026-05-12 -- never use as a change signal")

    print("\n[5] full cascade replay -> chassis exports NOTHING, loudly")
    tmp = os.path.join(tempfile.mkdtemp(), "t.db")
    st = store.Store(tmp)
    fake = FakeClient(vs, fixture("response_1-5.json"))
    run = export.Run(cfg, st, fake, "testrun")
    # V17 is a no-op, so stage1 stops there. Use V16 (real content) to reach stage2.
    st.set_state("version:" + chassis["did"], "none")
    v16 = vs[-2]
    targets = run.stage2_changed_drawings(chassis, v16)
    check("stage2 selects 0 drawings", len(targets) == 0, "all 3 fail the regex")
    check("all 3 skips recorded", len(st.skips_for_run("testrun")) == 3)
    check("skip reason is the naming gate",
          all(r["reason"] == "name does not match convention" for r in st.skips_for_run("testrun")))
    check("the unnumbered 'Chassis' part studio is NOT flagged",
          all("Tube" in r["subject"] for r in st.skips_for_run("testrun")),
          "an unnumbered source part studio is the norm, not a mistake")

    print("\n[5b] classifier: drawings vs part studios vs neither")
    kinds = {e["name"]: osapi.classify(e) for e in fixture("response_1-5.json")}
    check("part studio classified", kinds["Chassis"] == osapi.KIND_PARTSTUDIO)
    check("drawing classified", kinds['Tube 2"x1"x18.5" Drawing 1'] == osapi.KIND_DRAWING)
    check("assembly ignored", kinds["100 - Chassis"] is None)
    check("BOM ignored", kinds["BOM : 100 - Chassis"] is None)
    check("elementType=DRAWING still matches nothing",
          not [e for e in fixture("response_1-5.json") if e.get("elementType") == "DRAWING"])

    print("\n[5c] Component Type derives from the A prefix, free")
    check("part -> DWG", identity.parse("1250-26B-102").is_subassembly is False)
    check("subassembly -> ASM", identity.parse("1250-26A-A503").is_subassembly is True)

    print("\n[6] first-export defaulting (spec 9 -- the KeyError bug)")
    check("last_exported_microversion returns None, not KeyError",
          st.last_exported_microversion("never-seen-element-id") is None,
          "a renamed drawing must export on first sight")
    st.close()

    n = sum(1 for _, ok, _ in results if not ok)
    print("\n{} passed, {} failed  ({} API calls)".format(len(results) - n, n, 0))
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
