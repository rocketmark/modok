# Demo UI Specs

Specs for `ui/` — the local Next.js demo console for MODOK.

LLD: `docs/llds/demo-ui.md`

---

## Test Level Convention

- **[U]** — Unit test: component tests (Jest + React Testing Library) for UI behavior; Jest with mocked `fs` and `child_process` for API route behavior.
- **[E2E]** — End-to-end test (Playwright): browser-driven flow against a running Next.js dev server with mock mode enabled.

`[U]` is the minimum for all specs. `[E2E]` is required for specs that describe a user-visible flow crossing the browser/server boundary.

---

## Navigation

- [ ] **DEMO-NAV-001** [U]: The system shall redirect requests to `/` to `/tickets`.
- [ ] **DEMO-NAV-002** [U, E2E]: When `/tickets` is loaded and at least one ticket exists, the system shall redirect to `/tickets/<id>` of the newest ticket (highest `created_at`).
- [ ] **DEMO-NAV-003** [U, E2E]: While no tickets exist, `/tickets` shall display an empty state with a prompt to create the first ticket.
- [ ] **DEMO-NAV-004** [U, E2E]: When the user clicks a ticket card in the left panel, the system shall navigate to `/tickets/<id>` for that ticket.
- [ ] **DEMO-NAV-005** [U]: When `/tickets/<id>` is loaded and no ticket with that ID exists in `data/tickets.json`, the system shall display a "Ticket not found" message in the detail panel.

---

## Layout

- [ ] **DEMO-LAYOUT-001** [U]: The system shall render a top navigation bar on every page containing the MODOK wordmark and a search input.
- [ ] **DEMO-LAYOUT-002** [U]: The top navigation bar shall contain no left-side navigation links, section tabs, or workflow stage indicators.
- [ ] **DEMO-LAYOUT-003** [U, E2E]: `/tickets/<id>` shall render three panels: ticket list (left), ticket detail (center), MODOK analysis (right), all visible simultaneously without toggling.

---

## Ticket List Panel

- [ ] **DEMO-LIST-001** [U]: The ticket list panel shall display all tickets sorted by `created_at` descending (newest first).
- [ ] **DEMO-LIST-002** [U]: Each ticket card shall display the ticket ID, subject (truncated to fit), and formatted `created_at` date.
- [ ] **DEMO-LIST-003** [U]: Each ticket card shall display a MODOK status indicator reflecting the latest `ModokRun.status` for that ticket: "Not run", "Running", "Complete", or "Failed".
- [ ] **DEMO-LIST-004** [U]: The ticket card for the currently viewed ticket (`/tickets/<id>`) shall be visually highlighted.
- [ ] **DEMO-LIST-005** [U]: The ticket list panel shall display a "New Ticket" button at the bottom of the list.
- [ ] **DEMO-LIST-006** [U, E2E]: When the "New Ticket" button is clicked, the system shall open a modal for ticket creation without navigating away from the current ticket.

---

## New Ticket

- [ ] **DEMO-NEW-001** [U]: The new ticket modal shall contain a subject field (required) and a content field (optional).
- [ ] **DEMO-NEW-002** [U]: While the subject field is empty, the system shall not enable the modal's submit button.
- [ ] **DEMO-NEW-003** [U, E2E]: When the new ticket modal is submitted with a non-empty subject, the system shall POST to `/api/tickets`, close the modal, and navigate to the new ticket's detail view.
- [ ] **DEMO-NEW-004** [U]: When the modal is dismissed or cancelled, the system shall discard the form contents without creating a ticket.

---

## API: Ticket Creation and Listing

- [ ] **DEMO-TICK-API-001** [U]: `GET /api/tickets` shall return all tickets from `data/tickets.json` sorted newest-first, each merged with its latest `ModokRun` status from `data/modok-runs.json`.
- [ ] **DEMO-TICK-API-002** [U]: `POST /api/tickets` shall prepend the new ticket (with a generated ID and `created_at: now`) to `data/tickets.json` and return the created ticket.
- [ ] **DEMO-TICK-API-003** [U]: `GET /api/tickets/<id>` shall return the ticket, its notes (oldest-first), and its latest `ModokRun` from `data/modok-runs.json`.
- [ ] **DEMO-TICK-API-004** [U]: `GET /api/tickets/<id>` shall return HTTP 404 when no ticket with that ID exists in `data/tickets.json`.

---

## Ticket Detail Panel

- [ ] **DEMO-DETAIL-001** [U]: The detail panel shall display the ticket's subject, formatted `created_at` timestamp, and full content body.
- [ ] **DEMO-DETAIL-002** [U]: The detail panel shall display a chronological notes timeline showing each note's author, formatted timestamp, and body (oldest at top).
- [ ] **DEMO-DETAIL-003** [U]: While no notes exist for the ticket, the notes timeline shall display an empty state rather than a blank section.

---

## Notes

- [ ] **DEMO-NOTE-001** [U]: The detail panel shall display a note input field and an "Add" button below the notes timeline.
- [ ] **DEMO-NOTE-002** [U]: While the note body field is empty, the system shall not enable the Add button.
- [ ] **DEMO-NOTE-003** [U, E2E]: When the Add button is clicked with a non-empty body, the system shall POST to `/api/tickets/<id>/notes` and append the new note to the timeline without a full page reload.
- [ ] **DEMO-NOTE-004** [U]: While `ModokRun.status` for the current ticket is `"running"`, the note input and Add button shall be disabled.
- [ ] **DEMO-NOTE-005** [U]: `POST /api/tickets/<id>/notes` shall append the note (with generated ID and `created_at: now`) to `data/notes.json` and return the created note.
- [ ] **DEMO-NOTE-006** [U]: `POST /api/tickets/<id>/notes` shall return HTTP 400 when `body` is absent or empty.

---

## MODOK Panel

- [ ] **DEMO-MODOK-001** [U]: The MODOK panel shall display a "Build Debug Packet" button when the current ticket has no prior run or the current ticket's last run has `status: "failed"`.
- [ ] **DEMO-MODOK-002** [U]: While `ModokRun.status` is `"running"`, the MODOK panel shall replace the button with a spinner and a progress label.
- [ ] **DEMO-MODOK-003** [U]: While `ModokRun.status` is `"running"`, the "Build Debug Packet" button shall not be clickable.
- [ ] **DEMO-MODOK-004** [U, E2E]: When `ModokRun.status` transitions to `"complete"`, the MODOK panel shall render the debug packet without a full page reload.
- [ ] **DEMO-MODOK-005** [U]: The debug packet shall render sections in this order: Issue Summary, Anchors, Relevant Docs, Code Files, Known Issues, Prior Fixes, Raw JSON.
- [ ] **DEMO-MODOK-006** [U]: Sections of the debug packet that contain no items shall be omitted entirely from the rendered output.
- [ ] **DEMO-MODOK-007** [U]: The Raw JSON section shall be a collapsible block containing a `<pre>`-formatted JSON string, collapsed by default.
- [ ] **DEMO-MODOK-008** [U]: When `ModokRun.status` is `"failed"`, the MODOK panel shall display an error message corresponding to `ModokRun.error` (see error key table in LLD).
- [ ] **DEMO-MODOK-009** [U]: When `ModokRun.ingest_partial` is `true`, the MODOK panel shall display a warning banner above the debug packet reading "Ingestion completed with errors — results may be incomplete."
- [ ] **DEMO-MODOK-010** [U]: When `ModokRun.error` is `"parse_error"`, the MODOK panel shall render the Raw JSON section with the raw stdout content even though it is not valid JSON.

---

## MODOK Bridge (API Route)

- [ ] **DEMO-BRIDGE-001** [U]: When `ui/config.json` is absent or missing `project_slug` or `modok_source`, all API routes shall return HTTP 503 with the message "Demo not configured — create `ui/config.json` with `project_slug` and `modok_source`."
- [ ] **DEMO-BRIDGE-002** [U]: When `MODOK_MOCK=1` is set, `POST /api/tickets/<id>/modok` shall return a fixture `ModokRun` loaded from `data/mock-debug-packets.json` without invoking `child_process.spawn`.
- [ ] **DEMO-BRIDGE-003** [U]: Before spawning any process, `POST /api/tickets/<id>/modok` shall write `{ status: "running", ran_at: <now> }` to `data/modok-runs.json` for the ticket.
- [ ] **DEMO-BRIDGE-004** [U]: The bridge shall spawn `modok ingest` using `child_process.spawn` with `shell: false` and an explicit string array of arguments — no shell string concatenation.
- [ ] **DEMO-BRIDGE-005** [U]: The bridge shall spawn `modok retrieve` using `child_process.spawn` with `shell: false` and an explicit string array of arguments — no shell string concatenation.
- [ ] **DEMO-BRIDGE-006** [U]: Each spawn (ingest and retrieve) shall be terminated with SIGTERM after 60 seconds if it has not exited, and the bridge shall write `{ status: "failed", error: "timeout" }` and return it.
- [ ] **DEMO-BRIDGE-007** [U]: If either spawn fails with `ENOENT`, the bridge shall write `{ status: "failed", error: "modok_not_found" }` and return it.
- [ ] **DEMO-BRIDGE-008** [U]: If `modok ingest` exits with code 2, the bridge shall write `{ status: "failed", error: "quine_unreachable" }` and return it without spawning `modok retrieve`.
- [ ] **DEMO-BRIDGE-009** [U]: If `modok ingest` exits with code 3 (partial success), the bridge shall proceed to spawn `modok retrieve` and set `ingest_partial: true` on the resulting `ModokRun`.
- [ ] **DEMO-BRIDGE-010** [U]: If `modok retrieve` exits with code 1, the bridge shall write `{ status: "failed", error: "issue_not_found" }` and return it.
- [ ] **DEMO-BRIDGE-011** [U]: If `modok retrieve` exits with code 2, the bridge shall write `{ status: "failed", error: "quine_unreachable" }` and return it.
- [ ] **DEMO-BRIDGE-012** [U]: If `modok retrieve` exits with code 0 but stdout is not parseable as JSON, the bridge shall write `{ status: "failed", error: "parse_error", raw_stdout: <stdout> }` and return it.
- [ ] **DEMO-BRIDGE-013** [U]: Before writing the ticket markdown file, the bridge shall create `demo-data/customer-tickets/` if it does not exist, using a recursive `mkdir`.
- [ ] **DEMO-BRIDGE-014** [U, E2E]: When `modok retrieve` exits 0 with valid JSON stdout, the bridge shall write `{ status: "complete", debug_packet: <parsed>, ingest_exit_code, ingest_stderr, retrieve_exit_code, retrieve_stderr }` to `data/modok-runs.json` and return it.

---

## Stale Run Detection

- [ ] **DEMO-STALE-001** [U]: When `data/modok-runs.json` is read and a run has `status: "running"` and `ran_at` more than 5 minutes before the current time, the system shall overwrite that entry with `{ status: "failed", error: "timeout_or_crash" }` before returning the data. (Note: this is distinct from DEMO-BRIDGE-006, which kills an active spawn at 60 seconds during a live run. Stale detection recovers persisted `running` state left behind if the server process crashed before the spawn completed.)
- [ ] **DEMO-STALE-002** [U]: Stale run detection shall apply on every read of `data/modok-runs.json` (ticket list load, ticket detail load, and post-run response).

---

## Search

- [ ] **DEMO-SEARCH-001** [U, E2E]: When the user submits a non-empty query in the top nav search input (Enter key or submit button), the system shall call `GET /api/search?q=<query>` and display results in an overlay below the input.
- [ ] **DEMO-SEARCH-002** [U]: Each search result in the overlay shall display a node type badge, the node name or slug, and a truncated summary.
- [ ] **DEMO-SEARCH-003** [U]: The search overlay shall close when the user presses Escape, clicks outside the overlay, or navigates to a ticket.
- [ ] **DEMO-SEARCH-004** [U]: When the search input is empty and submitted, the system shall make no request and shall clear any existing overlay results.
- [ ] **DEMO-SEARCH-005** [U]: Clicking a search result shall close the overlay and display the node's detail (type, ID, summary) in a sheet panel.
- [ ] **DEMO-SEARCH-006** [U]: `GET /api/search?q=<query>` shall spawn `modok search --project <slug> <query> --json` with `shell: false` and return the parsed JSON stdout.
- [ ] **DEMO-SEARCH-007** [U]: `GET /api/search` shall return HTTP 400 when `q` is absent or empty.

---

## Config

- [ ] **DEMO-CFG-001** [U]: `lib/config.ts` shall read `ui/config.json` and validate that `project_slug` (non-empty string) and `modok_source` (non-empty string) are present.
- [ ] **DEMO-CFG-002** [U]: When `ui/config.json` passes validation, `lib/config.ts` shall return a typed config object without throwing.
- [ ] **DEMO-CFG-003** [U]: When `MODOK_MOCK=1` is set in the environment, the bridge shall use mock mode regardless of the values in `ui/config.json`.
- [ ] **DEMO-CFG-004** [U, E2E]: When any page or component receives an HTTP 503 response from an API route, the system shall display a full-panel error banner containing the message from the response body in place of the normal page content.

---

## Data Persistence

- [ ] **DEMO-DATA-001** [U]: `data/tickets.json` shall be a JSON array of `Ticket` objects; all read and write operations shall preserve the array's newest-first sort order.
- [ ] **DEMO-DATA-002** [U]: `data/notes.json` shall be a JSON array of `Note` objects; notes for a ticket shall be retrieved by filtering on `ticket_id` and returned oldest-first.
- [ ] **DEMO-DATA-003** [U]: `data/modok-runs.json` shall be a JSON object mapping `ticket_id` (string) to `ModokRun`; all reads and writes shall treat missing keys as `{ status: "not_run" }`.
- [ ] **DEMO-DATA-004** [U]: `data/tickets.json`, `data/notes.json`, and `data/modok-runs.json` shall be checked-in files; no API route shall truncate or regenerate them from scratch.
