# Demo UI

## Context and Design Philosophy

The Demo UI is a local web console that makes MODOK's debug-packet workflow tangible. Its sole purpose is to demonstrate the core loop: open a customer ticket, click **Build Debug Packet**, watch `ingest + retrieve` run, and see the result. It is not a production application.

Guiding principles:
- **The debug packet is the product.** Every layout decision prioritizes making the packet readable and credible.
- **Developer aesthetic.** Light mode, calm palette, cards with thin borders. Feels like Linear or Vercel, not a support portal.
- **No left navigation.** A top bar carries MODOK branding and a global search. Nothing else competes with the content.
- **Subprocess bridge, not embedded library.** The Next.js API routes call the `modok` CLI via `child_process.spawn`. This keeps the Node process and the Python process cleanly separated. Errors surface through exit codes and stderr, not exceptions.
- **SSE streaming for retrieve.** `modok retrieve` emits NDJSON progress lines. The bridge reads them line-by-line and pushes partial `DebugPacket` updates to the client via a `ReadableStream`, so the UI renders candidates before the LLM summary arrives.
- **Mock mode for demos without Quine.** `MODOK_MOCK=1` returns a fixture packet, making the UI demoable offline.

## Tech Stack

- **Next.js 14** (App Router), TypeScript, Tailwind CSS
- **shadcn/ui** for base components (cards, badges, buttons, inputs)
- **Local JSON files** under `ui/data/` for ticket and note persistence
- No database, no auth, no deployment target

## Directory Layout

```
ui/
  config.json                  # user-editable: project_slug, modok_source
  data/
    tickets.json               # seeded ticket records (checked in)
    notes.json                 # notes keyed by ticket ID (starts empty, checked in)
    modok-runs.json            # latest run result per ticket ID (starts empty, checked in)
    mock-debug-packets.json    # fixture packet for mock mode (checked in)
  demo-data/
    customer-tickets/          # markdown files written before ingest (gitignored)
  src/
    app/
      layout.tsx               # top nav, global shell
      page.tsx                 # redirects to /tickets
      tickets/
        page.tsx               # ticket list (redirects to first ticket or shows empty state)
        [id]/
          page.tsx             # three-panel: list + detail + MODOK
      api/
        tickets/
          route.ts             # GET /api/tickets, POST /api/tickets
          [id]/
            route.ts           # GET /api/tickets/[id]
            notes/
              route.ts         # POST /api/tickets/[id]/notes
            modok/
              route.ts         # POST /api/tickets/[id]/modok
        search/
          route.ts             # GET /api/search?q=<query>
    components/
      nav/
        TopNav.tsx
        SearchBar.tsx
        SearchResults.tsx
      tickets/
        TicketCard.tsx
        TicketList.tsx
        NewTicketModal.tsx
        NotesTimeline.tsx
        NoteInput.tsx
      modok/
        ModokPanel.tsx
        RunButton.tsx
        ProgressState.tsx
        DebugPacketView.tsx
        PacketSection.tsx
        RawJsonCollapsible.tsx
    lib/
      data.ts                  # read/write helpers for local JSON files
      modok-bridge.ts          # spawn wrapper, mock mode logic, stale-run detection
      config.ts                # load ui/config.json, validate required fields
      markdown.ts              # render ticket + notes to markdown string
    types/
      ticket.ts
      debug-packet.ts
```

## Visual Layout

### Top Navigation

```
┌──────────────────────────────────────────────────────────────────────┐
│  MODOK    [  Search graph...                                       ]  │
└──────────────────────────────────────────────────────────────────────┘
```

Left: MODOK wordmark. Center: search input (full width). No right-side elements in v1.

### `/tickets/[id]` — Three-Panel Layout

The canonical view. The left panel is a narrow persistent ticket list (newest on top, selected ticket highlighted). The center panel is ticket detail. The right panel is the MODOK analysis output.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  MODOK    [  Search...                                                    ]  │
├──────────────┬──────────────────────────────┬──────────────────────────────┤
│ ACME-1842  ● │  ACME-1842                   │  MODOK Analysis              │
│ Checkout f…  │  Checkout fails after…       │                              │
│ Jan 15       │  Jan 15, 2024 10:32 AM       │  [Build Debug Packet]        │
│              │                              │                              │
│ GLOBEX-991 ✓ │  Content                     │  ─────────────────────────── │
│ Webhook re…  │  When a user applies a gift  │  Anchors                     │
│ Jan 14       │  card with zero balance,     │  Features: checkout, payment │
│              │  checkout enters a retry     │                              │
│ INITRODE-237 │  loop and fails with a       │  Relevant Docs               │
│ Search res…  │  misleading error.           │  · Checkout flow design      │
│ Jan 12       │                              │  · Payment retry policy      │
│              │  Notes                       │                              │
│ UMBRELLA-404 │  ┌──────────────────────┐    │  Code Files                  │
│ User invite… │  │ Jan Doe · 10:32 AM   │    │  · src/checkout/retry.py     │
│ Jan 10       │  │ Customer confirmed…  │    │                              │
│              │  └──────────────────────┘    │  Known Issues                │
│ STARK-733  ● │                              │  · Payment retry max…        │
│ Dashboard …  │  [Add a note…        ] [Add] │                              │
│ Jan 8        │                              │  Prior Fixes                 │
│              │                              │  · Fix payment retry count   │
│  [+ New]     │                              │                              │
│              │                              │  ▶ Raw JSON                  │
└──────────────┴──────────────────────────────┴──────────────────────────────┘
```

`/tickets` redirects to `/tickets/<id>` of the first (newest) ticket. If no tickets exist, it shows an empty state with a "New Ticket" prompt. The `[+ New]` button is at the bottom of the left panel and opens a modal.

## Data Model

### Ticket

```typescript
interface Ticket {
  id: string;          // e.g. "ACME-1842"
  subject: string;     // one-line summary
  content: string;     // free-form description body
  created_at: string;  // ISO 8601
}
```

Tickets are stored as a JSON array in `data/tickets.json`, sorted newest-first. New tickets are prepended (unshift). The ticket schema is intentionally minimal — no severity, status, customer, or source fields. All demo tickets share the same `modok_source` from `ui/config.json`.

### Note

```typescript
interface Note {
  id: string;
  ticket_id: string;
  author: string;
  body: string;
  created_at: string;  // ISO 8601
}
```

Notes are stored as a flat array in `data/notes.json`. Filtered client-side by `ticket_id`. Appended chronologically — notes within a ticket are always oldest-first.

### ModokRun

```typescript
interface ModokRun {
  ticket_id: string;
  status: "not_run" | "running" | "complete" | "failed";
  debug_packet?: DebugPacket;
  ingest_exit_code?: number;
  ingest_stderr?: string;
  retrieve_exit_code?: number;
  retrieve_stderr?: string;
  ran_at?: string;    // ISO 8601; set when status transitions to "running"
  mock?: boolean;
  error?: string;     // error key when status is "failed"
}
```

`modok-runs.json` is a map of `ticket_id → ModokRun`. Status is updated by the API route; `running` is written before spawn begins. The ticket list panel reads this to show MODOK status indicators.

**Stale run detection:** On any read of `modok-runs.json`, if a run has `status: "running"` and `ran_at` is more than 5 minutes ago, it is treated as `failed` with `error: "timeout_or_crash"` and written back. This prevents a crash or timeout mid-spawn from leaving the ticket permanently stuck at "Running."

### Config

```json
{
  "project_slug": "stagehand",
  "modok_source": "demo-crm"
}
```

`project_slug` is the `--project` argument for all `modok` CLI calls. `modok_source` is the `--source` argument for `modok retrieve`. Both fields are required.

If `ui/config.json` is absent or missing required fields, `lib/config.ts` throws a `ConfigError`. All API routes catch this and return HTTP 503 with the message: `"Demo not configured — create ui/config.json with project_slug and modok_source."` The UI surfaces this as a full-panel error banner on first load.

`MODOK_MOCK=1` env var enables mock mode regardless of config contents.

## API Routes

| Route | Method | Description |
|---|---|---|
| `/api/tickets` | GET | Returns all tickets (newest-first) merged with MODOK status |
| `/api/tickets` | POST | Creates a new ticket; prepends to `data/tickets.json`; body: `{ subject, content }` |
| `/api/tickets/[id]` | GET | Returns ticket, notes, and latest ModokRun |
| `/api/tickets/[id]/notes` | POST | Appends a note; body: `{ author, body }` |
| `/api/tickets/[id]/modok` | POST | Runs ingest + retrieve; returns ModokRun |
| `/api/search` | GET | Runs `modok search`; query param `q`; returns node list |

## MODOK Bridge (`lib/modok-bridge.ts`)

`POST /api/tickets/[id]/modok` sequence:

1. Load and validate `ui/config.json`. Return 503 if missing or malformed.
2. Load ticket from `data/tickets.json`. Return 404 if not found.
3. Load notes for the ticket from `data/notes.json`.
4. If `MODOK_MOCK=1`: load fixture from `data/mock-debug-packets.json`, write a `complete` ModokRun, return it.
5. Write `{ status: "running", ran_at: <now> }` to `data/modok-runs.json` for this ticket.
6. Render ticket + notes to markdown. Ensure `demo-data/customer-tickets/` exists (`fs.mkdirSync(..., { recursive: true })`). Write markdown to `demo-data/customer-tickets/<ticket-id>.md`.
7. Spawn: `modok ingest --project <slug> demo-data/customer-tickets/<ticket-id>.md`
   - `shell: false`, explicit args array, 60s timeout. On timeout: SIGTERM, write `failed / timeout`, return `{ error: "timeout" }`.
   - Capture stdout and stderr. Wait for exit.
   - Exit 2: write `failed`, return `{ error: "quine_unreachable" }`. Do not proceed.
   - Exit 3: set `ingest_partial = true`. Proceed to retrieve.
8. Spawn: `modok retrieve --project <slug> --source <modok_source> --ticket <ticket-id>` via `spawnLineStreaming`.
   - `shell: false`, explicit args array, 60s timeout. On timeout: SIGTERM, write `failed / timeout`.
   - Each stdout line is parsed as NDJSON `{ step, data }`. Lines where `step !== "complete"` are emitted as `{ type: "partial", packet: data }` to the client stream, enabling incremental rendering.
   - Exit 1: write `failed`, return `{ error: "issue_not_found" }`.
   - Exit 2: write `failed`, return `{ error: "quine_unreachable" }`.
   - Exit 0 with no valid final packet: if a partial packet was received, return it as best-effort `complete`; otherwise write `failed / parse_error`.
9. Write `complete` ModokRun (debug packet, both exit codes, both stderrs, `ingest_partial` flag) to `data/modok-runs.json`.
10. Return the ModokRun.

Spawn fails with `ENOENT`: write `failed`, return `{ error: "modok_not_found" }`.

**Note input during a run:** The note input and Add button are disabled while `status === "running"`. This prevents the race where a note added after step 6 would be absent from the ingested markdown.

## Search

`GET /api/search?q=<query>` spawns:

```
modok search --project <slug> <query> --json
```

Returns `{ project: string, nodes: SearchNode[] }` from stdout.

The `SearchBar` fires on form submit (Enter or click). Results render in a `SearchResults` overlay below the search input. Each result shows: node type badge, name/slug, truncated summary. The overlay closes on Escape, outside click, or ticket navigation. Clicking a result shows node detail in a sheet (type, ID, summary).

**Debounce:** The search input fires on submit only — not on keystroke. No debounce needed; the user explicitly submits. Empty query: no request, overlay cleared.

## Ticket Markdown Format

Written to `demo-data/customer-tickets/<ticket-id>.md` before ingestion:

```markdown
# Ticket: <id>

**Source:** <modok_source>
**Created:** <created_at>

## Subject

<subject>

## Content

<content>

## Notes

**<created_at> — <author>**
<body>

**<created_at> — <author>**
<body>
```

The Notes section is omitted if there are no notes. Each note is separated by a blank line.

## Error States

| Condition | UI behavior |
|---|---|
| `config_missing` | Full-panel banner: "Demo not configured — create `ui/config.json`." |
| `modok_not_found` (ENOENT) | "modok CLI not found. Is it installed and on PATH?" |
| `quine_unreachable` (exit 2) | "MODOK could not reach Quine. Try: `modok quine start`" |
| `issue_not_found` (exit 1) | "MODOK could not find this issue in the project graph." |
| `timeout` | "MODOK timed out after 60s." |
| `parse_error` | "MODOK returned unexpected output." Raw stdout shown in collapsible. |
| Ingest partial (exit 3) | Warning banner above packet: "Ingestion completed with errors — results may be incomplete." |
| Stale `running` status | Treated as `failed` automatically; ticket list shows failed indicator. |

## Debug Packet Rendering

The MODOK panel renders sections in this order:

1. **LLM Summary** — `debug_packet.summary`; shown only when non-empty; slate-50 callout box above the issue title.
2. **Issue title** — `debug_packet.issue.summary` as bold text.
3. **Anchors** — features, errors, symptoms as labelled lines; section omitted if all three are empty.
4. **Affected Areas** — feature/module slugs as pill badges (⬡ for feature, ○ for module).
5. **Top Suspects** — `scored_candidates` rendered as `CandidateRow` items (see below).
6. **Code Files** — `relevant_files` as monospace chips.
7. **Test Files** — `relevant_tests` as monospace chips.
8. **Known Issues** — summary text per issue.
9. **Prior Fixes** — commit SHA chip + summary per fix.
10. **Recent Commits** — SHA chip, date, author, message for each commit.
11. **Raw JSON** — collapsible `<pre>` block; collapsed when `summary` is non-empty, open otherwise; shown even on `parse_error` (raw stdout).

Sections with no content are omitted entirely.

### CandidateRow

Each `ScoredCandidate` renders as a list item with:

- **Header row**: confidence badge (colour-coded: red=high, amber=medium, slate=low), file path (monospace, truncated), score.
- **Evidence list** — items other than `recent_commit` and `function_anchor_match` render as `· {type} {explanation}`. Penalty items (score < 0) are rendered in orange with the score shown.
- **Recent commits block** — if any `recent_commit` evidence exists:
  - Label row: `· recent_commit`
  - All commits listed below the label, left-aligned at the same indent.
  - Most recent commit: SHA, date, author, function match annotation (if any), commit message (truncated to 60 chars).
  - Additional commits: same format, rendered in a lighter colour.
  - Function match annotation (`· fn: {names}`) is shown inline on whichever commit's SHA matches a `function_anchor_match` explanation.
- `function_anchor_match` evidence items are not shown as standalone rows; their function name and SHA are extracted and displayed inline with the matching commit row.

The SHA-to-metadata lookup uses `recent_commits` from the packet keyed by 7-character SHA prefix.

## Seed Tickets

Five tickets checked in to `data/tickets.json`, ordered newest-first. Each has a `subject` and a short `content` paragraph:

| ID | Subject |
|---|---|
| ACME-1842 | Checkout fails after retrying payment |
| GLOBEX-991 | Webhook replay causes duplicate notification |
| INITRODE-237 | Search results ignore archived filter |
| UMBRELLA-404 | User invite email links to expired token |
| STARK-733 | Dashboard metrics lag after deploy |

`data/notes.json` and `data/modok-runs.json` are checked in as empty objects `{}`. `data/mock-debug-packets.json` is checked in with one realistic fixture packet.

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|---|---|---|---|
| CLI invocation method | `child_process.spawn`, no shell, explicit args array | `exec` with shell string | Eliminates shell injection. `spawn` gives separate stdout/stderr streams. |
| CLI call strategy | Ingest is synchronous (wait for exit); retrieve uses `spawnLineStreaming` for NDJSON SSE progress | All-sync; all-async | Ingest has no streaming output. Retrieve emits partial packets; streaming lets the UI render candidates while the LLM summary generates. |
| `function_anchor_match` display | Inline with the matching commit row, not as a standalone evidence item | Separate evidence row | Showing function name next to the commit that introduced it is more readable than an orphaned evidence line. |
| `doc_penalty` evidence colour | Orange text with score shown | Same grey as positive evidence | Penalty items are negative signals; distinguishing them visually prevents confusion with positive evidence. |
| Ticket schema | Minimal: id, subject, content, created_at | Full CRM schema with severity, status, customer, source | This is a demo shell, not a ticketing product. Minimal schema is faster to fill, easier to read, and puts focus on the MODOK output. |
| Ticket sort order | Newest-first (array prepend on create) | Oldest-first; manual reorder | Newest-on-top is the natural inbox pattern. New tickets are the active work. |
| Ticket persistence | Local JSON files in `ui/data/` | SQLite; in-memory only | JSON files are inspectable and portable. No native binary dependency. In-memory loses state on restart. |
| Three-panel layout | Ticket list (left) + detail (center) + MODOK panel (right) always visible | Two-column; single column; panels behind tabs | All three panels are always useful during a demo. No navigation required to switch context. Matches the reference mockup. |
| Config mechanism | `ui/config.json` (user-editable) + `MODOK_MOCK` env var | Only env vars; only config file | Config file for stable demo settings; env var for transient mode toggle. |
| MODOK status persistence | `ui/data/modok-runs.json` | In-memory server state; client localStorage | Server-side JSON survives browser refresh and Next.js restart. localStorage can't be read by API routes. |
| Stale run recovery | Auto-recover runs > 5 min old as `failed` on read | Manual reset button; no recovery | Silent recovery prevents a confusing permanent "Running" state without requiring user action. 5 min is well beyond the 60s spawn timeout. |
| Spawn timeout | 60s per spawn, SIGTERM on timeout | No timeout; 30s; 120s | 60s is generous for local `modok` calls. Prevents indefinite hangs. |
| Search trigger | Submit only (Enter / button click) | Debounced keystroke search | `modok search` is a subprocess call, not a fast autocomplete endpoint. Submit-on-Enter matches the cost model. |
| `demo-data/` directory | Created on-demand by bridge (`mkdirSync recursive`) | Pre-created; setup script | One less setup step. The bridge always knows the path; creating it is cheap. |
| Seed data format | Checked-in static JSON files | Generated by setup script; seeded at app startup | Static files are version-controlled, always present, and don't require a setup step or startup side effect. |
| Note input during run | Disabled while `status === "running"` | Allow notes; include in next run | Prevents the race where a note is added after the markdown is written but before ingest runs. |

## Open Questions & Future Decisions

### Resolved
1. ✅ SSE streaming for retrieve — `spawnLineStreaming` emits partial packets; UI renders candidates before summary arrives.

### Deferred
1. **Search result → ticket linking** — show a "View ticket" link in the result sheet when a node is referenced by a known ticket. Deferred: requires indexing node-to-ticket relationships.
2. **Mock packet variety** — one global fixture vs. one fixture per ticket. Deferred: one global fixture is fine for v1.

## References

- `docs/high-level-design.md` — Demo UI component description
- `docs/UI-brainstorm.md` — original UI technical spec
- `docs/ui-brainstorm-new.md` — visual direction and product framing
- `docs/llds/cli.md` — `modok search`, `modok ingest`, `modok retrieve` command specs
- `docs/assets/ui-mock.png` — reference mockup
