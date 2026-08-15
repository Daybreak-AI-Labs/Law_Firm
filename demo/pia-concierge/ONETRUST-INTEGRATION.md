# OneTrust integration — the verified API reference

This is the wire protocol the PIA Concierge speaks to OneTrust, as
implemented in `onetrust_client.py` and enforced by the mock tenant in
`ot_mock.py`. **Every endpoint and write shape below was verified against a
live OneTrust tenant** (a partner build drove real assessments end-to-end
with a static API key); the mock reproduces the tenant's behavior including
its traps, so the test suite proves wire compatibility offline. Point
`ONETRUST_HOSTNAME` + `ONETRUST_TOKEN` at a real tenant and the same calls
file there.

The agent is autonomous **only up to "Under Review"** — that stage is the
human-in-the-loop gate. Nothing in this client can mark an assessment
Complete.

## Auth

`Authorization: Bearer <API key>` (Access Management → Client Credentials →
API Keys). Grant the full scope set; POLICY/POLICY_READ are needed for the
notice cross-check.

## Assessments

| Operation | Endpoint | Notes |
|---|---|---|
| Launch | `POST /api/assessment/v3/assessments` | Requires `orgGroupId` **and** `respondentEmail`, or the tenant refuses. |
| Search/list | `POST /api/assessment/v3/assessments/list?page=&size=&assessmentStatuses=` with body `{visibleColumns:[…], filterCriteria:[]}` | ⚠ Pagination is inconsistent — pages can overlap. **Dedupe by `assessmentId`.** |
| Read | `GET /api/assessment/v2/assessments/{id}/export` | The plain `/{id}` resource returns **403** even for records the key can otherwise touch — always read via `/export`. Works for others' completed assessments too. |
| Write answers | `POST /api/assessment/v2/assessments/{id}/responses` | **Export first** — every write needs the question's `sectionId`, existing `responseId`, and option ids from a fresh export. Shapes below. |
| Submit | `POST /api/assessment/v2/assessments/{id}/submit?disclaimerAccepted=true` | Advances to Under Review. See the completeness gate below. |
| Link | `POST /api/assessment/v2/assessments/assessment-links` `{fromId, toIds:[…]}` | Ties an AI Model Assessment to its primary. |
| Reopen | `POST …/{id}/reopen` | **Session-gated** — 403 for a static key when the state disallows. Do it in the UI. |
| Delete | `DELETE …/{id}` | **Session-gated** (403 `ACCESS-MANAGEMENT-ASSERTIONS`) — clean up in the UI. |

### Response value shapes (verified)

* **Select / Yes-No** (this class of tenant models Yes/No as options):
  `{responseId: <optionId>, response: <optionText>,
  responseKey: "<translationId>.option.option", type: "DEFAULT"}` —
  a bare text answer is rejected.
* **Text**: `{responseId: <existing>, response: "<text>", type: "DEFAULT"}`.
* **Inventory dropdown / record selection**: `{responseId: <recordId>,
  response: <recordName>, type: "DEFAULT"}` — this writes the question
  answer (not just the primary record).
* **Justification**: an **additional entry in the same `responses[]`
  array** — `{responseId: null, response: "<p>…</p>", type:
  "JUSTIFICATION"}` (HTML). A plain top-level `justification` field is
  **silently dropped**; required questions then fail submit with no hint.
* **PERSONAL_DATA**: an array of rows, each
  `{response: null, responseId: null, type: "DEFAULT", responseMap:
  {DATA_CATEGORIES: {id,name,nameKey}, DATA_SUBJECTS: {…},
  DATA_ELEMENTS: {…}}}` — one full triple per row. Do **not** send the
  mirror element/subject/category nodes the export shows
  (`INVALID_REQUEST_INPUT`); `personalDataDetailId` is omittable for new
  rows; **a write replaces the whole row set** — send all triples at once.

### The completeness gate (and its under-reporting)

A 403 `INVALID_USER_OPERATION` on submit means a required question is
missing its **answer**, its **justification**, or a blank **inventory
dropdown**. The server's `requiredUnansweredQuestionIds` **under-reports**:
it omits justification-only misses and inventory dropdowns. The client
therefore computes completeness locally from the export
(`OneTrustClient.outstanding()`) and reports the full list.

After submit, **self-check the actual stage** by re-reading the export
(`status == "Under Review"`) — never trust that the POST returning means the
stage advanced. The client retries once (writing only material it actually
has — a justification, personal-data rows); it **never fabricates a
compliance answer**.

## Inventory + personal-data catalogs

* Records: `GET /api/inventory/v2/inventories/{schema}?page=&size=50` —
  the server-side name filter is **ignored** for static keys; page and
  fuzzy-match client-side (exact → unique prefix → unique substring →
  unique fuzzy; ambiguity re-lists, never guesses).
* **Vendors** may be created on a true no-match
  (`POST /api/inventory/v2/inventories/vendors`); **entities are never
  created** — pick-only, by policy and by tenant.
* Catalogs for the personal-data resolver:
  `GET /api/inventory/v2/data-elements` (each element embeds its
  `categories[]`), `/data-subjects`, `/data-categories`.

## Attachments (deliverables)

Two steps: (1) multipart `POST /api/document/v2/attachments` (file part +
`attachment` JSON `{FileName, Type:"10", RefIds:[recordId], IsInternal,
Encrypt}`) → `201 {Id}`; (2) link with
`POST /api/inventory/v2/inventories/{recordId}/attachments`
`[{attachmentId, name}]` → 201.

Attaching to an **assessment question** is impossible with a static key —
it is browser-session-gated (the `__Secure-fgpt` cookie bcrypt-matched to
the token's `fgpt` claim; a static key gets 403
`ACCESS-MANAGEMENT-ASSERTIONS_ACCESS_VIOLATION`, which is **not** a
permission/scope issue). Deliverables therefore go on the assessment's
primary **vendor record** (its Documents tab).

## Privacy notices (Enterprise Policy module)

The real published notices — **not** `/api/privacynotice/v2`, which is the
consent module's resource set.

* List: `GET /api/enterprise-policy/v1/{complianceType}/list?lastPublishedDate=YYYY-MM-DD&page=&size=50`
  where `complianceType ∈ privacynotices | policies | standards |
  procedures`. `lastPublishedDate` is required — use `1970-01-01` for all.
  Returns `{data:[{guid, organizationName (= brand), name, …}],
  meta:{page:{totalPages}}}`.
* Content: `GET …/privacynotices/{guid}/details` →
  `{versions:[{versionStatus, sections:[{name, content(HTML), order}]}]}`.
  The bare `/{guid}` and `/{guid}/versions` are the 403-gated **edit**
  resources — `/details` is the read view. Strip HTML → text.

The notice cross-check (`notice_check.py`) is brand-first and
cost-conscious: the contracting entity is the authoritative brand signal;
a unique name match picks the notice deterministically, sibling notices are
flagged for manual review (never auto-reviewed). A contradiction with an
affirmative promise (assessment says data is sold; notice says "we do not
sell") is a **critical** finding that escalates the filed rating; gaps are
GDPR Art. 13/14 items rendered into the recommendations document attached
to the vendor record.

## Known limitations (documented, not hidden)

* Assessment-level attach, reopen, and delete are session-gated (above) —
  attach to the vendor record; clean up in the UI.
* The assessment-search API paginates inconsistently — dedupe by id.
* Multi-select writes (e.g. an EU AI Act scope question on some AI
  templates) can return `INVALID_REQUEST_INPUT` — that template class uses
  its own response shape; export the question first to determine it before
  writing.
* Autonomous finalize only auto-submits when local completeness passes and
  personal data is written; anything still outstanding is reported
  verbatim — never invented.
