"""Stage A: Onshape -> PDF. The detection cascade from drawing-export-spec.md 4.2.

    STAGE 0  folder modifiedAt gate        1 call    (proven, spec 4.3)
    STAGE 1  version gate                  1/doc     purpose + no-op microversion
    STAGE 2  drawing gate                  1/changed dataType + regex + microversionId
    STAGE 3  export                        ~5/drawing

Five of six gates are free -- they run on data already fetched.

Run:
    py export.py --dry-run              replay against fixtures, 0 API calls
    py export.py --doc chassis          one document (~1 call instead of 6)
    py export.py                        all documents

Console output is ASCII-only: the Windows console is cp1252 and a stray arrow
raises UnicodeEncodeError, crashing the run (spec 10).
"""

import argparse
import json
import os
import sys
import uuid

import identity
import osapi
import store

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SECRETS = os.path.join(HERE, "secrets.json")


def resolve(path):
    """Resolve a config path relative to this file, so a fresh clone just works.

    Absolute paths pass through unchanged.
    """
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(HERE, path))


def load_credentials():
    """Return (access_key, secret_key).

    Env vars win; otherwise secrets.json (gitignored, never committed).
    Onshape caps individual accounts at 2 active API keys -- if you lose these,
    revoke and reissue at My Account -> Developer rather than making a third.
    """
    ak = os.environ.get("ONSHAPE_ACCESS_KEY")
    sk = os.environ.get("ONSHAPE_SECRET_KEY")
    if ak and sk:
        return ak, sk
    if os.path.exists(SECRETS):
        with open(SECRETS, encoding="utf-8") as f:
            s = json.load(f)
        ak, sk = s.get("access_key", ""), s.get("secret_key", "")
        # Treat blank/placeholder as absent so an unfilled template fails with a
        # clear message instead of an opaque 401.
        if not ak.strip() or not sk.strip() or "PASTE" in ak.upper():
            return None, None
        return ak.strip(), sk.strip()
    return None, None

# Onshape auto-versions (external-reference refreshes) carry purpose != 0.
# INFERRED from 2 samples (chassis V4, V13); `purpose` has no enum in the v16
# spec. Skips are logged so a wrong guess is visible, not silent. Spec 4.5a.
PURPOSE_USER_CREATED = 0


class Run:
    def __init__(self, cfg, st, client, run_id):
        self.cfg, self.store, self.api, self.run_id = cfg, st, client, run_id
        self.exported, self.skipped, self.failed = [], [], []

    def skip(self, stage, subject, reason):
        self.store.log_skip(self.run_id, stage, subject, reason)
        self.skipped.append((stage, subject, reason))

    # -- STAGE 0 ---------------------------------------------------------

    def stage0_changed_docs(self, docs):
        """One call -> which documents got a new version since last run."""
        try:
            items = self.api.folder_documents(self.cfg["onshape"]["folder_parent_id"])
        except osapi.OnshapeError as e:
            print("  stage0: folder query failed ({}); falling back to all docs".format(e))
            return docs

        by_did = {d["id"]: d for d in items}
        changed = []
        for doc in docs:
            node = by_did.get(doc["did"])
            if node is None:
                # Not in the folder -- cannot gate it, so never skip it.
                changed.append(doc)
                continue
            seen = self.store.get_state("modifiedAt:" + doc["did"])
            if seen and seen == node.get("modifiedAt"):
                self.skip("stage0", doc["key"], "modifiedAt unchanged")
                continue
            doc["_modifiedAt"] = node.get("modifiedAt")
            changed.append(doc)
        return changed

    # -- STAGE 1 ---------------------------------------------------------

    def stage1_new_version(self, doc):
        """Return the newest version worth exporting, or None."""
        vs = self.api.versions(doc["did"])
        if not vs:
            self.skip("stage1", doc["key"], "no versions")
            return None

        newest = vs[-1]  # ascending by createdAt (verified, spec 4.4)

        if self.store.get_state("version:" + doc["did"]) == newest["id"]:
            self.skip("stage1", doc["key"], "no new version since last run")
            return None

        if newest.get("purpose", 0) != PURPOSE_USER_CREATED:
            self.skip(
                "stage1",
                "{}/{}".format(doc["key"], newest.get("name")),
                "auto-version (purpose={})".format(newest.get("purpose")),
            )
            return None

        # A version whose microversion matches its parent's contains NO content
        # change. Expected to be common: versions here exist to publish to the
        # top-level assembly, not because drawings changed. Spec 4.5b.
        if len(vs) >= 2 and newest.get("microversion") == vs[-2].get("microversion"):
            self.skip(
                "stage1",
                "{}/{}".format(doc["key"], newest.get("name")),
                "no-op version (microversion identical to parent)",
            )
            self.store.set_state("version:" + doc["did"], newest["id"])
            return None

        return newest

    # -- STAGE 2 ---------------------------------------------------------

    def stage2_changed_drawings(self, doc, version):
        out = []
        for el in self.api.drawings_at_version(doc["did"], version["id"]):
            ident = identity.parse(el["name"])
            if ident is None:
                # The regex GATES export (spec 4.8) -- deliberately, to enforce
                # the numbering scheme. Loudly, so it teaches rather than hides.
                self.skip("stage2", el["name"], "name does not match convention")
                continue

            if ident.subsystem != doc.get("subsystem"):
                # Free sanity check: doc '300 - Intake' holding a 1250-26B-5NN
                # drawing is probably misfiled. Log, don't block.
                self.skip(
                    "stage2",
                    ident.id,
                    "subsystem {} but lives in doc {} ({}) -- misfiled?".format(
                        ident.subsystem, doc.get("subsystem"), doc["key"]
                    ),
                )

            # MUST default, not subscript: a drawing exporting for the first time
            # (incl. one just renamed into convention) has no prior row. Spec 9.
            last = self.store.last_exported_microversion(el["id"])
            if last is not None and last == el["microversionId"]:
                self.skip("stage2", ident.id, "unchanged since last export")
                continue

            out.append((el, ident))
        return out

    # -- STAGE 3 ---------------------------------------------------------

    def stage3_export(self, doc, version, el, ident):
        src = version["id"]
        # version.creator is free -- Stage 1 already fetched it. It attributes the
        # VERSION, not the drawing; see the schema comment in store.py.
        creator = version.get("creator") or {}
        self.store.record_drawing_state(
            source_id=src,
            source_kind="version",
            element_id=el["id"],
            document_id=doc["did"],
            document_key=doc["key"],
            document_name=doc.get("name"),
            element_name=el["name"],
            identifier=ident.id,
            version_id=version["id"],
            version_name=version.get("name"),
            microversion=el["microversionId"],
            configuration=None,
            creator_id=creator.get("id"),
            creator_name=creator.get("name"),
            observed_at=store.now(),
        )

        if self.store.already_exported(el["id"], src):
            self.skip("stage3", ident.id, "already exported at this version")
            return

        self.store.begin_export(el["id"], src)  # PENDING before POST -- resumable
        try:
            t = self.api.start_pdf_export(doc["did"], version["id"], el["id"])
            done = self.api.poll_translation(t["id"])
            fids = done.get("resultExternalDataIds") or []
            if not fids:
                raise osapi.OnshapeError("translation DONE but no resultExternalDataIds")

            data, digest = self.api.download_external(doc["did"], fids[0])

            folder = os.path.join(resolve(self.cfg["output"]["root"]), ident.bot_folder)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, ident.filename)
            with open(path, "wb") as f:
                f.write(data)

            self.store.finish_export(
                el["id"], src, "PDF", "DONE",
                translation_id=t["id"], output_path=path,
                sha256=digest, byte_size=len(data), last_error=None,
            )
            self.exported.append((ident.id, path, len(data)))
        except osapi.QuotaExhausted:
            raise
        except Exception as e:
            self.store.finish_export(el["id"], src, "PDF", "FAILED", last_error=str(e))
            self.failed.append((ident.id, str(e)))

    # -- driver ----------------------------------------------------------

    def execute(self, docs, use_stage0=True):
        docs = self.stage0_changed_docs(docs) if use_stage0 else docs
        for doc in docs:
            before = len(self.failed)
            try:
                self._execute_doc(doc)
            except (osapi.QuotaExhausted, osapi.BudgetExceeded):
                raise  # global stops: no point continuing
            except Exception as e:
                # One bad document must not abandon the rest -- calls already
                # spent on earlier docs would be wasted, and a transient 429 on
                # doc 2 shouldn't hide changes in docs 3-5.
                self.failed.append((doc["key"], str(e)))
                continue
            # Cursors advance only if THIS document had no failures.
            if len(self.failed) == before and doc.get("_version_done"):
                self.store.set_state("version:" + doc["did"], doc["_version_done"])
                if doc.get("_modifiedAt"):
                    self.store.set_state("modifiedAt:" + doc["did"], doc["_modifiedAt"])

    def _execute_doc(self, doc):
        version = self.stage1_new_version(doc)
        if version is None:
            return
        for el, ident in self.stage2_changed_drawings(doc, version):
            self.stage3_export(doc, version, el, ident)
        doc["_version_done"] = version["id"]


def report(run, calls):
    """'Nothing exported' must never be confusable with 'nothing changed'."""
    print("\n=== run summary ===")
    print("  exported : {}".format(len(run.exported)))
    for ident, path, size in run.exported:
        print("      {}  ({:,} bytes)  {}".format(ident, size, path))

    if run.failed:
        print("  FAILED   : {}".format(len(run.failed)))
        for ident, err in run.failed:
            print("      {}  {}".format(ident, err))

    naming = [s for s in run.skipped if s[2] == "name does not match convention"]
    if naming:
        print("  skipped (name doesn't match convention): {}".format(len(naming)))
        for _, subj, _ in naming:
            print("      {!r}".format(subj))
        print("      -> rename to <team>-<YY><bot>-[A]<S><NN> to include these")

    other = [s for s in run.skipped if s[2] != "name does not match convention"]
    if other:
        print("  skipped (other): {}".format(len(other)))
        for stage, subj, reason in other:
            print("      [{}] {}: {}".format(stage, subj, reason))

    print("  API calls counted this run: {}".format(calls))


def main():
    ap = argparse.ArgumentParser(description="Export changed Onshape drawings to PDF.")
    ap.add_argument("--doc", action="append", help="limit to document key(s); cheapest lever")
    ap.add_argument(
        "--stage0",
        action="store_true",
        help="use the folder modifiedAt gate. OFF by default: the folder holds ~40-60 "
        "documents, so it costs 3 paginated calls, not 1 -- a net loss (spec 4.3).",
    )
    ap.add_argument("--dry-run", action="store_true", help="refuse all live calls")
    ap.add_argument("--config", default=CONFIG,
                    help="alternate config, e.g. config.sandbox.json (spec 5b.5)")
    ap.add_argument("--db", default="state.db")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    docs = cfg["documents"]
    if args.doc:
        keys = set(args.doc)
        docs = [d for d in docs if d["key"] in keys]
        if not docs:
            sys.exit("no documents match {}".format(sorted(keys)))

    st = store.Store(args.db)
    spent = st.calls_counted()
    cap = cfg["budget"]["max_calls_per_cycle"]
    warn = cfg["budget"]["warn_at_cycle_calls"]
    print("quota: {} counted calls logged locally (cap {}, warn {})".format(spent, cap, warn))
    if spent >= cap:
        sys.exit("local budget cap reached ({}); refusing to run".format(cap))
    if spent >= warn:
        print("WARNING: past {} calls this cycle -- reserve headroom for build season".format(warn))

    run_id = uuid.uuid4().hex[:12]
    ak, sk = load_credentials()
    client = osapi.Client(cfg, st, run_id, access_key=ak, secret_key=sk, dry_run=args.dry_run)
    if not args.dry_run and not client._auth:
        sys.exit(
            "No credentials.\n"
            "  Create secrets.json (gitignored) next to export.py:\n"
            '      {"access_key": "...", "secret_key": "..."}\n'
            "  Get keys at: https://cad.onshape.com/appstore/dev-portal  (My Account -> Developer)\n"
            "  Or set ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY. Or pass --dry-run."
        )

    if args.dry_run:
        print("\ndry-run: live calls are refused. This flag is a SAFETY GUARD, not a")
        print("simulator -- for offline validation run:  py tests/test_replay.py")
        print("(replays real captured fixtures through the full cascade, 0 API calls)")

    run = Run(cfg, st, client, run_id)
    code = 0
    try:
        run.execute(docs, use_stage0=args.stage0)
    except osapi.QuotaExhausted as e:
        print("\n*** {} ***".format(e))
        code = 2
    except osapi.BudgetExceeded as e:
        print("\n*** local budget stop: {} ***".format(e))
        code = 2
    except osapi.OnshapeError as e:
        print("\n*** stopped: {} ***".format(e))
        code = 1

    report(run, client.calls_this_run)
    st.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
