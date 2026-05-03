Build a local demo web UI for MODOK.

Use Next.js, React, TypeScript, and Tailwind. The app is a customer-ticket console that shows off MODOK’s debugging workflow.

Core concept:
MODOK gives AI agents a running start when debugging software issues. This UI should show a handful of customer tickets. A user can open a ticket, add notes, and click “Run MODOK”. The backend should materialize the ticket into a markdown file, run the MODOK CLI, then render the returned debug packet.

Pages:

1. /tickets
- Show a clean list of seeded customer tickets.
- Each ticket card should include:
  - ticket id
  - title
  - customer
  - source system
  - severity
  - status
  - short description
  - MODOK status: Not run, Running, Complete, Failed
- Include a button to add a new ticket.

2. /tickets/[id]
- Show ticket detail view.
- Left column:
  - ticket metadata
  - description
  - error text
  - affected feature/module if present
  - notes timeline
  - input box to add a new note
- Right column:
  - MODOK panel
  - “Run MODOK” button
  - progress state while running
  - formatted debug packet after completion
  - collapsible raw JSON output

MODOK backend route:

POST /api/tickets/[id]/modok

Behavior:
1. Load the ticket and notes.
2. Render them into markdown at:
   demo-data/customer-tickets/<ticket-id>.md
3. Run:
   modok ingest --project stagehand demo-data/customer-tickets/<ticket-id>.md
4. Then run:
   modok retrieve --project stagehand --source demo-crm --ticket <ticket-id>
5. Parse retrieve stdout as JSON.
6. Return the parsed debug packet plus command metadata:
   - ingest exit code
   - ingest stderr
   - retrieve exit code
   - retrieve stderr

Use child_process.spawn, not shell string concatenation.

Add a mock mode:
- If process.env.MODOK_MOCK === "1", do not call the CLI.
- Instead return a realistic fixture debug packet from data/mock-debug-packets.json.
- This allows the UI to be demoed without Quine running.

Seed tickets:
- ACME-1842: Checkout fails after retrying payment
- GLOBEX-991: Webhook replay causes duplicate notification
- INITRODE-237: Search results ignore archived filter
- UMBRELLA-404: User invite email links to expired token
- STARK-733: Dashboard metrics lag after deploy

Design:
- Make it polished but developer-oriented.
- Use cards, badges, timelines, and collapsible sections.
- Do not make it look like a generic support desk.
- The key hero moment is clicking “Run MODOK” and seeing a debug packet appear.

Debug packet rendering sections:
- Issue summary
- Anchors
- Relevant docs
- Relevant code/files
- Known issues
- Prior fixes
- Confidence
- Raw JSON

Error handling:
- If Quine is down, show:
  “MODOK could not reach Quine. Try: modok quine start”
- If the ticket is not found, show:
  “MODOK could not find this issue in the project graph.”
- If ingestion returns partial success, still attempt retrieval but show a warning banner.

Do not build authentication.
Do not use a real database.
Persist tickets and notes in local JSON files under ./data.
