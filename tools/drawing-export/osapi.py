"""Onshape REST client. stdlib only -- no `requests`, so students can clone and run.

Every claim encoded here is verified against the LIVE v16 spec
(https://cad.onshape.com/api/v16/openapi, version 1.217.82698) or a real captured
response. Do NOT trust github.com/onshape-public/onshape-clients -- it is a 2021
snapshot (spec 1.113) and is wrong. See CLAUDE.md.

QUOTA: EDU Educator = 2,500 calls/YEAR, shared company-wide. Every 2xx/3xx costs
one. 4xx/5xx are free. This client logs and caps everything.
"""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

# --- verified constants (spec 4.6; confirmed in fixtures/response_1-5.json) ---
# Drawings are APPLICATION elements, NOT elementType=DRAWING. `DRAWING` is a legal
# GBTElementType value that no drawing reports, and the query param is an
# unvalidated bare string -- a wrong value returns [] with no error. This exact
# mistake cost a round trip; see CLAUDE.md.
DRAWING_ELEMENT_TYPE = "APPLICATION"
DRAWING_DATA_TYPE = "onshape-app/drawing"


class QuotaExhausted(Exception):
    """HTTP 402. A wall, not a throttle. Never retry."""


class BudgetExceeded(Exception):
    """Local cap hit. Prevents a bug from burning the team's season."""


class OnshapeError(Exception):
    pass


class Client:
    def __init__(self, cfg, store, run_id, access_key=None, secret_key=None, dry_run=False):
        self.base = cfg["onshape"]["base_url"].rstrip("/")
        self.cfg = cfg
        self.store = store
        self.run_id = run_id
        self.dry_run = dry_run
        self.calls_this_run = 0
        self._auth = None
        if access_key and secret_key:
            raw = "{}:{}".format(access_key, secret_key).encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()

    # -- core ------------------------------------------------------------

    def _request(self, method, path, params=None, body=None, raw=False):
        if self.dry_run:
            raise OnshapeError("dry_run: refusing live call to {}".format(path))

        cap = self.cfg["budget"]["max_calls_per_run"]
        if self.calls_this_run >= cap:
            raise BudgetExceeded("run cap {} reached".format(cap))

        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        if self._auth:
            req.add_header("Authorization", self._auth)

        try:
            with urllib.request.urlopen(req, timeout=self.cfg["http"]["timeout_seconds"]) as r:
                status, payload = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, payload = e.code, e.read()
            # 4xx/5xx are quota-exempt -- logged as counted=0.
            self.store.log_call(self.run_id, path, status)
            if status == 402:
                raise QuotaExhausted(
                    "402: annual API quota exhausted (2,500/yr, shared company-wide). "
                    "HALT. Do not retry -- retrying cannot help and masks a team-wide event."
                )
            if status == 429:
                retry = int(e.headers.get("Retry-After", "60"))
                raise OnshapeError("429 rate limited; Retry-After={}s".format(retry))
            raise OnshapeError("{} {} -> {}: {}".format(method, path, status, payload[:200]))

        counted = self.store.log_call(self.run_id, path, status)
        self.calls_this_run += counted
        return payload if raw else json.loads(payload)

    # -- Stage 0: folder gate (spec 4.3, PROVEN) --------------------------

    # GET /documents enforces limit <= 20 (jakarta @Max). limit=50 returns 400:
    # "must be less than or equal to 20". Not in the v16 spec's param schema --
    # found only by calling it. Free to discover (4xx is quota-exempt).
    FOLDER_PAGE_MAX = 20

    def folder_documents(self, parent_id, limit=FOLDER_PAGE_MAX):
        """One call -> every doc in the folder with modifiedAt.

        Creating a version bumps document.modifiedAt (proven by experiment);
        workspace edits do NOT. So this sees version events and ignores WIP.

        Paginates if the folder ever exceeds 20 entries. Today it holds 5, so
        this is a single call -- but a silent truncation at 20 would mean a
        document never exports, which is this tool's worst failure mode.
        """
        limit = min(limit, self.FOLDER_PAGE_MAX)
        items, offset = [], 0
        while True:
            r = self._request(
                "GET", "/documents",
                {"parentId": parent_id, "limit": limit, "offset": offset},
            )
            page = r.get("items", [])
            items.extend(page)
            if len(page) < limit or not r.get("next"):
                return items
            offset += limit

    def get_document(self, did):
        return self._request("GET", "/documents/{}".format(did))

    # -- Stage 1: versions ------------------------------------------------

    def versions(self, did):
        """Ascending by createdAt -- versions[-1] is NEWEST (verified, spec 4.4).

        Do not use limit=1 expecting the latest; it returns the OLDEST.
        """
        return self._request("GET", "/documents/d/{}/versions".format(did))

    # -- Stage 2: elements ------------------------------------------------

    def elements_at_version(self, did, vid):
        """EVERY element at a version. One call -- no elementType filter.

        We fetch unfiltered and classify client-side (see classify()) because we
        care about two kinds now:
          - drawings      (APPLICATION + onshape-app/drawing) -> export a PDF
          - part studios  (PARTSTUDIO)                        -> issue only, no PDF
        Asking Onshape to pre-filter would cost a second call to get the other
        kind, and the response is small either way. Same 1 call per document.
        """
        els = self._request("GET", "/documents/d/{}/v/{}/elements".format(did, vid))
        # `deleted` is in the v16 schema but ABSENT from real responses.
        # e["deleted"] would KeyError on every element. Always .get().
        return [e for e in els if not e.get("deleted", False)]


# Element kinds we act on. Everything else (assemblies, BOMs, blobs, CAM
# studios, feature studios) is ignored.
KIND_DRAWING = "drawing"
KIND_PARTSTUDIO = "partstudio"


def classify(el):
    """-> KIND_DRAWING | KIND_PARTSTUDIO | None.

    Drawings are APPLICATION elements with dataType 'onshape-app/drawing' --
    NOT elementType=DRAWING, which is a legal enum value that no drawing reports
    (spec 4.6). The dataType check also excludes CAM Studio, which is APPLICATION
    too and which the Educator plan includes.
    """
    et, dt = el.get("elementType"), el.get("dataType")
    if et == DRAWING_ELEMENT_TYPE and dt == DRAWING_DATA_TYPE:
        return KIND_DRAWING
    if et == "PARTSTUDIO":
        return KIND_PARTSTUDIO
    return None

    # -- Stage 3: export --------------------------------------------------

    def start_pdf_export(self, did, vid, eid):
        """POST /drawings/d/{did}/{wv}/{wvid}/e/{eid}/translations

        {wv} accepts a workspace OR A VERSION. Every doc page and forum post
        shows only /w/{wid}, implying versioned drawings can't be exported.
        They can. This claim carries the whole architecture (spec 2).
        """
        body = {
            "formatName": "PDF",
            # false => no PDF blob tabs written back into released documents
            "storeInDocument": False,
            # false => multi-sheet drawings export as a single PDF
            "currentSheetOnly": False,
        }
        return self._request(
            "POST", "/drawings/d/{}/v/{}/e/{}/translations".format(did, vid, eid), body=body
        )

    def poll_translation(self, tid):
        """Poll until DONE/FAILED with exponential backoff.

        Each *successful* poll costs a quota call, so back off generously.
        """
        h = self.cfg["http"]
        delay = h["poll_initial_seconds"]
        for _ in range(h["poll_max_attempts"]):
            r = self._request("GET", "/translations/{}".format(tid))
            state = r.get("requestState")
            if state == "DONE":
                return r
            if state == "FAILED":
                raise OnshapeError("translation FAILED: {}".format(r.get("failureReason")))
            time.sleep(delay)
            delay = min(delay * h["poll_backoff_factor"], h["poll_max_seconds"])
        raise OnshapeError("translation {} did not finish in {} polls".format(tid, h["poll_max_attempts"]))

    def download_external(self, did, fid):
        data = self._request(
            "GET", "/documents/d/{}/externaldata/{}".format(did, fid), raw=True
        )
        return data, hashlib.sha256(data).hexdigest()
