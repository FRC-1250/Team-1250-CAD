"""Stage B: PDF -> git commit -> GitHub issue comment.

Decoupled from Stage A by design (spec 5b). Stage A is quota-constrained and
fragile; this side is effectively free and retryable. SQLite is the seam -- this
re-runs freely against PDFs already paid for, and a GitHub failure can never burn
Onshape quota.

    py publish.py --dry-run          show what would happen, touch nothing
    py publish.py                    commit PDFs + comment on matched issues
    py publish.py --create-issues    also OPEN issues for unmatched parts

Auto-create is OFF by default. Creating issues is outward-facing, visible to the
whole team, and awkward to undo. As of 2026-07-16 no issue body contains a
1250-26B-style part number, so EVERY drawing is currently unmatched -- enabling
this blind would open one issue per drawing, some duplicating existing ones
(e.g. #91 'Chassis Tubes' already covers the chassis tube drawings).
Run once without it, read the report, then decide.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

import github_api
import identity
import store

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SECRETS = os.path.join(HERE, "secrets.json")


def resolve(path):
    """Config paths are relative to this file, so a fresh clone just works."""
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(HERE, path))


def git(repo, *args, check=True):
    p = subprocess.run(
        ["git"] + list(args), cwd=repo, capture_output=True, text=True
    )
    if check and p.returncode != 0:
        raise RuntimeError("git {}: {}".format(" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def comment_body(ident, blob_url, version_name, doc_name):
    return (
        "**Drawing PDF updated** -- `{id}`\n\n"
        "| | |\n|---|---|\n"
        "| Part | `{id}` |\n"
        "| Subsystem | {sub} |\n"
        "| Onshape document | {doc} |\n"
        "| Version | {ver} |\n\n"
        "[View PDF]({url})\n\n"
        "<sub>Posted automatically by `tools/drawing-export`. The link is pinned to "
        "the commit, so it always shows this version of the drawing.</sub>"
    ).format(id=ident.id, sub=ident.subsystem, doc=doc_name, ver=version_name, url=blob_url)


def issue_body(ident, blob_url):
    return (
        "### Component Description\n\n"
        "_Auto-created by `tools/drawing-export` for part `{id}`, which had a drawing "
        "but no tracking issue._\n\n"
        "Subsystem {sub}. Please fill in a real description.\n\n"
        "### Notes / Links\n\n"
        "[Drawing PDF]({url})\n\n"
        "<sub>The part number `{id}` is in this issue's title so future drawing "
        "exports link here automatically.</sub>"
    ).format(id=ident.id, sub=ident.subsystem, url=blob_url)


def main():
    ap = argparse.ArgumentParser(description="Publish exported PDFs to GitHub.")
    ap.add_argument("--dry-run", action="store_true", help="report only; touch nothing")
    ap.add_argument("--create-issues", action="store_true",
                    help="OPEN issues for unmatched parts (off by default -- read the docstring)")
    ap.add_argument("--push", action="store_true", help="push the commit (default: commit locally only)")
    ap.add_argument("--db", default="state.db")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    repo = resolve(cfg["output"]["repo_path"])
    owner, name = cfg["output"]["repo_url"].rstrip("/").split("/")[-2:]

    token = None
    if os.path.exists(SECRETS):
        token = (json.load(open(SECRETS, encoding="utf-8")).get("github_token") or "").strip() or None
    token = os.environ.get("GITHUB_TOKEN", token)

    st = store.Store(args.db)
    rows = st.db.execute(
        "SELECT e.element_id, e.source_id, e.output_path, ds.identifier, ds.version_name, "
        "       ds.document_name "
        "FROM export e JOIN drawing_state ds "
        "  ON ds.element_id=e.element_id AND ds.source_id=e.source_id "
        "LEFT JOIN publish p "
        "  ON p.element_id=e.element_id AND p.source_id=e.source_id AND p.format=e.format "
        "WHERE e.status='DONE' AND (p.status IS NULL OR p.status NOT IN ('LINKED','COMMITTED')) "
    ).fetchall()

    if not rows:
        print("nothing to publish (no DONE exports awaiting publication)")
        return 0

    print("{} PDF(s) to publish".format(len(rows)))
    if not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit("repo not found at {} -- clone it first".format(repo))

    gh = github_api.GitHub(owner, name, token)
    if not args.dry_run and not token:
        sys.exit(
            "No GitHub token.\n"
            '  Add "github_token": "ghp_..." to secrets.json (gitignored),\n'
            "  or set GITHUB_TOKEN. Needs repo scope. Or pass --dry-run."
        )

    # --- stage files -------------------------------------------------------
    staged, plan = [], []
    for r in rows:
        ident = identity.parse(r["identifier"] or "")
        if ident is None:
            print("  WARN: stored identifier {!r} no longer parses; skipping".format(r["identifier"]))
            continue
        rel = "{}/{}".format(ident.bot_folder, ident.filename)
        dest = os.path.join(repo, ident.bot_folder, ident.filename)
        hits = gh.find_by_identifier(ident.id)
        plan.append((r, ident, rel, dest, hits))

    print("\n=== plan ===")
    for r, ident, rel, dest, hits in plan:
        where = ("issue #{}".format(hits[0]["number"]) if len(hits) == 1
                 else "AMBIGUOUS: {}".format([h["number"] for h in hits]) if hits
                 else ("CREATE new issue" if args.create_issues else "NO MATCH -- no issue, PDF still committed"))
        print("  {}  ->  {}   [{}]".format(ident.id, rel, where))

    unmatched = [p for p in plan if not p[4]]
    if unmatched and not args.create_issues:
        print("\n  {} part(s) have no matching issue.".format(len(unmatched)))
        print("  Their PDFs will still be committed and linkable -- only the comment is skipped.")
        print("  To open issues for them, re-run with --create-issues (read publish.py's docstring first:")
        print("  no issue currently contains a 1250-26B-style number, so nothing will match yet).")

    if args.dry_run:
        print("\ndry-run: nothing written.")
        return 0

    # --- commit ------------------------------------------------------------
    for r, ident, rel, dest, hits in plan:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(r["output_path"], dest)
        git(repo, "add", rel)
        staged.append((r, ident, rel, hits))

    if not git(repo, "status", "--porcelain"):
        print("\nno changes to commit (PDFs byte-identical to what's in the repo)")
        return 0

    msg = "Add drawing PDFs: {}".format(", ".join(i.id for _, i, _, _ in staged))
    git(repo, "commit", "-m", msg)
    sha = git(repo, "rev-parse", "HEAD")
    print("\ncommitted {} ({} file(s))".format(sha[:8], len(staged)))

    if args.push:
        git(repo, "push")
        print("pushed to {}".format(cfg["output"]["repo_url"]))
    else:
        print("NOT pushed (use --push). Commit-pinned links are only valid once pushed.")

    # --- link --------------------------------------------------------------
    for r, ident, rel, hits in staged:
        blob = "{}/blob/{}/{}".format(cfg["output"]["repo_url"].rstrip("/"), sha, rel)
        try:
            if len(hits) == 1:
                url = gh.comment(hits[0]["number"], comment_body(
                    ident, blob, r["version_name"], r["document_name"]))
                st.db.execute(
                    "INSERT OR REPLACE INTO publish(element_id,source_id,format,status,identifier,"
                    "repo_path,commit_sha,blob_url,issue_number,comment_url,published_at) "
                    "VALUES(?,?,'PDF','LINKED',?,?,?,?,?,?,?)",
                    (r["element_id"], r["source_id"], ident.id, rel, sha, blob,
                     hits[0]["number"], url, store.now()),
                )
                print("  {} -> commented on #{}".format(ident.id, hits[0]["number"]))
            elif len(hits) > 1:
                st.db.execute(
                    "INSERT OR REPLACE INTO publish(element_id,source_id,format,status,identifier,"
                    "repo_path,commit_sha,blob_url,last_error,published_at) "
                    "VALUES(?,?,'PDF','AMBIGUOUS',?,?,?,?,?,?)",
                    (r["element_id"], r["source_id"], ident.id, rel, sha, blob,
                     "matches issues {}".format([h["number"] for h in hits]), store.now()),
                )
                print("  {} -> AMBIGUOUS ({}); commented on none".format(
                    ident.id, [h["number"] for h in hits]))
            elif args.create_issues:
                num, url = gh.create_issue(
                    "{} ".format(ident.id) + os.path.splitext(ident.filename)[0],
                    issue_body(ident, blob))
                st.db.execute(
                    "INSERT OR REPLACE INTO publish(element_id,source_id,format,status,identifier,"
                    "repo_path,commit_sha,blob_url,issue_number,comment_url,published_at) "
                    "VALUES(?,?,'PDF','LINKED',?,?,?,?,?,?,?)",
                    (r["element_id"], r["source_id"], ident.id, rel, sha, blob, num, url, store.now()),
                )
                print("  {} -> created issue #{}".format(ident.id, num))
            else:
                st.db.execute(
                    "INSERT OR REPLACE INTO publish(element_id,source_id,format,status,identifier,"
                    "repo_path,commit_sha,blob_url,published_at) "
                    "VALUES(?,?,'PDF','COMMITTED',?,?,?,?,?)",
                    (r["element_id"], r["source_id"], ident.id, rel, sha, blob, store.now()),
                )
                print("  {} -> committed, no issue matched".format(ident.id))
            st.db.commit()
        except github_api.GitHubError as e:
            print("  {} -> FAILED: {}".format(ident.id, e))
    st.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
