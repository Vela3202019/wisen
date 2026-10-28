# Cloud architecture: from mirror to Gardener

**Status.** Decided by the owner on 2026-10-18. This doc is the contract that
build steps 1–7 (below) are written against. If a later PR needs to break the
contract, it changes this doc in the same PR.

**Goal.** In the owner's words: "think bold. the goal is to make world-class
open source project - agent harness built from scratch. through which i learned
and explored design orchestration from scratch."

**Privacy rule.** "i care a lot about data privacy. i don't want to upload all
coding logs for user to cloud. the result can be in cloud tho."

**Where the decisions came from.** Three research reports written on
2026-10-18 (commercial cloud products, open agent harnesses, and cloud
infrastructure). They live outside this repo. This doc records only what the
owner decided from them, plus the questions still open
([Open questions](#14-open-questions)).

---

## 1. Summary

- wisen grows from a **mirror** (a daily reflection) into an agent that **acts
  on what it learns**. The acting role is the **Gardener**. It may change only
  `CLAUDE.md`, `AGENTS.md` and skills. Every change is a git branch, and a human
  approves every action. It never touches repo code.
- A small **server** joins the laptop. It is built in two stages ("yes a then b"):
  - **Stage A: local brain, server mailbox.** Every model call runs on the
    laptop. The server holds the schedule, the approval inbox, results and push
    notifications. It makes **no model calls**.
  - **Stage B: the NREM/REM split.** Connectors and the Observer (NREM) stay on
    the laptop and emit typed, redacted **packets**. The Synthesizer and the
    Gardener (REM) move to the server and see packets only.
- **The packet schema is the privacy boundary.** It is enforced in code at both
  ends: the laptop builds packets only through it, and the server rejects
  anything that does not match it.
- **pydantic-ai** (>=2.57.0) runs single agent calls. Everything between calls
  and between machines is built from scratch: a **SQLite job engine** and a
  **proposal state machine**. DBOS is the fallback engine. pydantic-graph is
  never used to save state (its persistence was removed in V2).
- **SQLite everywhere.** MongoDB is retired on the laptop too.
- The server is **one Python process under systemd** on the owner's Hetzner box,
  behind the Caddy that already serves HTTPS, at
  `wisen.vela-server.duckdns.org`. The laptop only ever connects **outbound**,
  with a revocable per-device token.
- You approve from **Web Push** (the dashboard as an installable web app) or
  **Telegram** (notifications, approve/reject buttons, and a chat bot that
  searches history).
- **Each developer self-hosts.** Nothing assumes one user: every stored row
  carries a `user_id`, so a hosted service stays possible later.

---

## 2. Where wisen is today

The design changes these parts of the current code:

| Today | File | What changes |
|---|---|---|
| MongoDB via `motor` for reflections, chat sessions and voice sessions | `wisen/db.py`, used by `wisen/main.py`, `wisen/cli.py`, `wisen/chat/agent.py`, `wisen/voice/agent.py`, `wisen/synthesis/agent.py`, `scripts/export_reflections.py` | Replaced by SQLite (step 1) |
| Chat history stored as text-only `{role, content}`, so tool calls and results are lost between turns | `wisen/main.py` (`/chat`), `wisen/chat/agent.py` | Stored as full pydantic-ai `Conversation`s (step 1) |
| Synthesis jobs in an in-memory dict that a restart loses | `wisen/main.py` (`_jobs`) | Durable job engine (step 3) |
| The daily run is a launchd one-shot | `scripts/run_synthesis.py` | Becomes a scheduled job; launchd only keeps the laptop daemon alive (steps 3–4) |
| A stdlib dashboard on the VPS: exported reflection JSON, a `*.req` file queue for triggers, one shared Basic-auth password | `wisen/dashboard/server.py` | Grows into the server (step 4) |
| What may leave the machine is one allow-list model, `CloudReflection`, guarded by a canary test | `wisen/synthesis/schema.py`, `wisen/export.py`, `tests/test_egress.py` | Becomes the first packet type (step 2) |
| The Observer sends each turn cut to 400 characters to the model provider | `wisen/synthesis/agent.py` (`_format_session`) | Cap may become configurable, up to about 4,000 characters (not yet scheduled; open question 12) |
| Recommendations are typed (`claude-md`, `agents-md`, …) but nothing acts on them | `wisen/synthesis/schema.py` (`Recommendation`) | The Gardener turns them into proposals (step 5) |
| The eval-loop plan auto-applies a config change on a branch when the replay eval improves | `docs/plan/eval-loop.md` (Phase 3) | Superseded: replay results become evidence on a proposal; a human still approves |

---

## 3. The shape: stage A, then stage B

### 3.1 Stage A: local brain, server mailbox

All model work over raw transcripts runs on the laptop, exactly where it runs
today. The server is a mailbox: it stores what the laptop sends, holds what
the human decides, and wakes the laptop up with work. It has no model API key.

```mermaid
flowchart LR
    subgraph LAPTOP["Laptop: raw transcripts live here"]
        direction TB
        SRC["Agent histories<br/>~/.claude, ~/.codex, …"] --> CON["Connectors + normalizer"]
        CON --> OBS["Observer (NREM)"]
        OBS --> SYN["Synthesizer (REM)"]
        SYN --> GAR["Gardener<br/>proposes changes"]
        ENG["Job engine<br/>SQLite: ~/.wisen/wisen.db"]
        APPLY["Applier<br/>git branch in the target repo"]
        EGR["Egress gate<br/>packet schema + redaction<br/>+ egress ledger"]
        SYN --> EGR
        GAR --> EGR
    end

    subgraph SERVER["Server: Hetzner box, one process, no model calls"]
        direction TB
        MAIL[("Mailbox, SQLite<br/>results, proposals, decisions,<br/>schedule, device jobs")]
        INBOX["Approval inbox<br/>+ dashboard"]
        PUSH["Web Push sender"]
        TG["Telegram bot"]
        MAIL --- INBOX
        MAIL --- PUSH
        MAIL --- TG
    end

    PROV(["Model provider"])
    OBS -. "excerpts (inference tier)" .-> PROV
    SYN -.-> PROV
    GAR -.-> PROV

    EGR == "packets only<br/>HTTPS, laptop-initiated,<br/>device token" ==> MAIL
    MAIL == "device jobs + decisions<br/>(laptop polls)" ==> ENG
    ENG --> APPLY

    PUSH --> PHONE["Phone / browser<br/>installed web app"]
    TG <--> TGAPP["Telegram app"]
    PHONE -- "approve / reject" --> INBOX
    TGAPP -- "approve / reject,<br/>questions" --> TG
```

Model calls in stage A: **laptop only** (Observer, Synthesizer, Gardener, chat
answers). The server's job is reach and time: it is awake when the laptop is
not, and it is where your phone can find you.

### 3.2 Stage B: NREM on the laptop, REM on the server

The README already describes the pipeline as sleep: the Observer is NREM
(replay the raw day), the Synthesizer is REM (find patterns). Stage B makes
that metaphor the privacy line. NREM touches raw episodes and stays home.
REM works only on consolidated memories (packets) and may run on the server,
so it can "dream" while the laptop sleeps.

```mermaid
flowchart LR
    subgraph LAPTOP["Laptop: raw transcripts live here"]
        direction TB
        SRC["Agent histories"] --> CON["Connectors + normalizer"]
        CON --> OBS["Observer (NREM)"]
        OBS --> EGR["Egress gate<br/>ObservationPacket"]
        SEARCH["Raw-history search<br/>(explicit requests only)"]
        SEARCH --> EGR
        ENG["Job engine, SQLite"]
        APPLY["Applier<br/>git branch"]
    end

    subgraph SERVER["Server: Hetzner box"]
        direction TB
        CHECK["Schema check<br/>rejects anything else"]
        STORE[("SQLite<br/>packets, reflections,<br/>proposals, jobs")]
        SYN["Synthesizer (REM)"]
        GAR["Gardener"]
        CHAT["Chat over reflections"]
        CHECK --> STORE
        STORE --> SYN --> STORE
        STORE --> GAR --> STORE
        STORE --- CHAT
        CHANNELS["Approval inbox, Web Push,<br/>Telegram"]
        STORE --- CHANNELS
    end

    PROVL(["Model provider<br/>(laptop's key)"])
    PROVS(["Model provider<br/>(server's key)"])
    OBS -. "excerpts" .-> PROVL
    SEARCH -.-> PROVL
    SYN -. "packets only" .-> PROVS
    GAR -.-> PROVS
    CHAT -.-> PROVS

    EGR == "packets<br/>laptop-initiated" ==> CHECK
    STORE == "approved changes,<br/>search requests" ==> ENG
    ENG --> APPLY
    ENG --> SEARCH
```

Model calls in stage B:

- **Laptop:** the Observer, and raw-history search when you ask for it
  explicitly. These are the only calls that see raw text.
- **Server:** the Synthesizer, the Gardener and reflection chat. They see
  packets only. The server now holds a model API key.

Approved changes are still **applied on the laptop**. The Gardener on the
server writes the proposal; the laptop's applier makes the branch.

**How it is built (step 7).** Both ends opt in: the server with
`WISEN_CLOUD_STAGE=b`, each laptop with `[cloud] stage = "b"` (the laptop
decides what leaves it). Upgrade the server first: an older server refuses
the new packet types.

- **Keeping the server supplied.** While awake, `wisen worker` runs the
  Observer on yesterday and today whenever a session ended after the notes it
  last sent (each day at most every 2 hours, since each run is a model call)
  and uploads them as an `ObservationPacket`. It also uploads a
  `ConfigSnapshotPacket` for each file the Gardener may edit that changed
  (read at most hourly). It no longer uploads reflections.
- **The morning run.** At the user's daily time the server queues an
  `observe` device job (an awake laptop sends fresh notes; the job hands it
  the server's previous reflection as the Observer's baseline) and its own
  synthesis job, due 30 minutes later. A laptop's answer brings the synthesis
  forward; with every laptop asleep it runs on the notes already uploaded.
  The synthesis job merges every laptop's notes on the day into one
  `DayObservation` (each text field keeps each laptop's text under its device
  name; lists are joined), runs the Synthesizer, runs the day's Gardener over
  the config snapshots, then publishes the reflection as a result packet, so
  the one push names the day's batch. The §5.5 skip holds on the server:
  no notes on the day, or a reflection newer than every note, is left alone.
  Notes that arrive after the run (a laptop that slept through it) run the
  day again. Every run, retry or rerun merges the notes afresh, so a run
  that failed and runs again takes in notes sent since; notes sent while a
  run works make it go round again. The server's reflection is stamped with
  the newest note it reflects on.
- **The same code on both ends.** Received result, metrics and measurement
  packets are mirrored into the tables the laptop's agents read
  (`reflections`, `daily_metrics`, `proposal_measures`), so the Synthesizer,
  the Gardener (with its past-proposal checks, placement and recurrence
  rules) and chat run unchanged; only file access differs. The server's
  Gardener reads the snapshot's redacted text and builds the same git-format
  diff without a repo. Replay is skipped there (it reads transcripts), and
  the proposal says so.
- **Applying.** A proposal drafted on the server names the laptop whose
  snapshot it came from. The decision feed carries the proposal's content to
  that laptop when it is approved; the laptop records it and the usual
  applier re-checks the approval, the allow-list and the base blob. Where a
  hunk touches a line the server saw redacted, the applier applies the diff to
  the redacted text and keeps the real text of every line the change does not
  add, so the approved lines are written exactly and no placeholder replaces
  a real value. A file whose redaction changes its line count (a multi-line
  secret) fails to apply, with the reason. The run resumes on the server once
  its proposals are decided.
- **Chat.** `POST /api/chat` on the server (confirmed by the owner on
  2026-10-28): logged-in browser session, stage B only (404 in stage A),
  JSON in, up to 50 messages of 8,000 characters, the answer streamed as
  server-sent events like the laptop's `/chat`. The same chat agent
  (`wisen/chat/agent.py`) answers from the server's reflections only, with no
  raw-history tools, and nothing is stored: the caller sends the
  conversation each turn. No dashboard UI calls it yet.
- **Models.** Synthesis runs on `claude -p` on the owner's subscription by
  default (`WISEN_SYNTH_MODEL`, `claude-cli:sonnet`); `WISEN_SYNTH_MODEL=api`
  is the fallback to the API model. The Gardener uses tools, so it runs on
  `WISEN_MODEL` and its API key (moving it to `claude -p` is a follow-up).
  Chat runs on DeepSeek (`WISEN_CHAT_MODEL`, `DEEPSEEK_API_KEY`; owner's
  decision): the reflections it reads, already redacted, reach DeepSeek.

### 3.3 What runs where

| Component | Local-only mode | Stage A | Stage B |
|---|---|---|---|
| Connectors, normalizer | laptop | laptop | laptop |
| Observer (NREM) | laptop | laptop | laptop |
| Synthesizer (REM) | laptop | laptop | **server** |
| Gardener: propose | laptop | laptop | **server** |
| Gardener: apply (git branch) | laptop | laptop | laptop |
| Gardener: measure (eval loop) | laptop | laptop | laptop (reads transcripts) |
| Chat over reflections | laptop | laptop, asked via the server | **server** |
| Raw-history search | laptop | laptop | laptop |
| Schedule, approval inbox, push, Telegram | laptop UI only, no push | server | server |
| Job engine | laptop | laptop + server | laptop + server |
| Model API key | laptop | laptop | laptop + server |

**Local-only mode stays first-class.** With no server configured, the laptop
runs everything and you approve in the local dashboard or CLI. The server is
an add-on, never a requirement.

---

## 4. The privacy line

### 4.1 Three tiers

The README's [What leaves your machine](../../README.md#what-leaves-your-machine)
section already names three tiers. This design keeps them and makes the third
one strict:

1. **Data at rest: local.** Raw transcripts stay where the agents wrote them.
   wisen stores derived data only, in `~/.wisen/wisen.db`.
2. **Model inference: excerpts go to the configured provider.** This is true
   today and stays true for the laptop's model calls. The owner allowed larger
   excerpts when they help: "up to 400 characters of every turn can be bigger if
   needed like 4000 char". The per-turn cap can become a setting (default 400,
   ceiling about 4,000). No build step delivers that yet (open question 12).
   Raising it is a measured change: run `wisen eval` before and after, and
   update the README numbers in the same PR.
3. **Cloud: packets only.** The server receives nothing that is not a packet.
   Results may include short verbatim quotes. The owner's call: "original quote
   is fine i guess bc it's not original transcription". Quotes are capped in
   length and redacted like other free-text fields.

A fully local option (an on-laptop model for the Observer, so no provider sees
raw text) is an open question, not part of this plan.

### 4.2 Packets

A packet is a pydantic model that is allowed to leave the laptop. The rule:
**if it is not a packet type, it does not cross.** `CloudReflection` in
`wisen/synthesis/schema.py` is already this pattern (fields listed explicitly,
so a new field stays local until someone decides it may leave). Step 2
generalizes it.

All packet types live in one module that both the laptop and the server import,
so the two ends can never disagree about the schema. From step 2, every
packet carries this envelope:

- `packet_type`
- `schema_version`
- `user_id`
- `packet_id`: a deterministic hash of its content, used for idempotent upload
- `created_at`

`device_id` joins the envelope in step 4, together with device tokens.

| Packet | Stage | Carries | Never carries |
|---|---|---|---|
| `ReflectionPacket` (today's `CloudReflection`) | A | The day's reflection: mood, questions, friction points, recommendations, insight, short evidence quotes | Stage-1 output, raw turns, file paths outside an alias |
| `ProposalPacket` | A | A Gardener proposal: target file (as `project alias + relative path`), the unified diff, rationale, evidence refs and short quotes, eval results if any, the daily batch it belongs to; for a proposal decided while unpaired, that decision or expiry and its note | The rest of the target repo |
| `ProposalUpdatePacket` | A (step 5) | A git state the laptop decided (`applied`, `failed`, `merged`, `abandoned`, `measuring`, `kept`, `revert_proposed`), the branch name, a short note (why it failed, why it was kept), and the measured numbers before and after | Repo paths, commit contents |
| `MetricsPacket` | A (step 5) | One day's verification-loop numbers ([§5.4](#54-verification-loop-climbing-the-hill)): user turns, corrections by detector, tokens, problems solved, human turns per PR | Sessions, prompts, file names |
| `JobStatusPacket` | A | Job id, state, timings, error class | Stack traces with local paths or content |
| `AnswerPacket` | A | A chat answer written by a model, with short quotes and dates | Raw search hits |
| `ObservationPacket` | B | The Observer's notes on one day from one laptop (`DayObservation`): mood and drift summaries, topics, friction seen, recommendations followed or not, model usage, key moments with capped quotes | Full turns, tool output, code |
| `ConfigSnapshotPacket` | B | One file the Gardener may edit: its `project alias + relative path`, the git blob id of its real text, and that text, secret-redacted; a gone file is sent once with neither. A file over 20,000 characters is left out, never cut | Any other file |

The egress gate on the laptop runs in this order:

1. **Select** only the fields the packet type lists (allow-list). Selecting
   from a stored local document may drop keys the packet does not list, as
   `CloudReflection` does today with `extra="ignore"`.
2. **Redact** free-text and quote values: API keys and tokens
   (secret-detector patterns), absolute home paths (replaced by a project
   alias), email addresses.
3. **Truncate** free-text and quote values, and lists, to their cap.

   Whole-value fields are never redacted or truncated: the `ProposalPacket`
   unified diff, `packet_id` and other identifiers and hashes, the
   `project alias + relative path` target, branch names and state values. They
   go through the same detectors and caps, but a value that redaction would
   change or that is over its cap **rejects the packet**: it stays on the
   laptop and the failure is reported. A human never approves a cut-short or
   rewritten diff.
4. **Validate** by constructing the strict packet model (`extra="forbid"`,
   `max_length` on every string and list). Validation only rejects, never
   shortens: an unknown field or an over-cap value here is a programming error.
5. **Record** the packet in the local egress ledger: `packet_id`, type, byte
   size, field names, hash, time sent. `wisen egress` prints it, so you can see
   what left your machine today.
6. **Send.**

The server validates the wire packet with the same `extra="forbid"` models and
the same caps, and **rejects** anything that fails. That is what "enforced at
both ends" means: a bug on the laptop cannot widen the boundary, because the
server will not store a field it does not know.

> 中文说明：「包」（packet）就是允许离开笔记本的唯一数据形状。笔记本只能用这些类型去打包，打包时先对自由文本和引用脱敏（密钥、绝对路径、邮箱）、再截断长度（diff、ID、路径这类必须完整的字段不改不截，命中就整包拒发、留在本地）、再用严格类型校验、再记一笔出境账本；服务器用同一份类型定义再检查一遍，不认识的字段直接拒收。所以就算笔记本这边有 bug，也没法把原始日志「顺带」传上去。

Durable journals are an egress risk of their own. A job step's saved output is
a stored copy of whatever the step returned. So the Observer step journals its
**packet output**, never the raw turns it read, and any laptop-side journal
that does hold raw text is pruned once its packet is built. Telemetry is
another path out: any agent run that sees raw text uses pydantic-ai
instrumentation with `include_content=False` if traces leave the laptop.

### 4.3 Proving it

`tests/test_egress.py` already plants marker strings and checks that none
reach the exported JSON. The same test grows with each step:

- Step 2: a fixture with a fake API key and an absolute path produces a packet
  with neither; the server-side validator rejects a packet with an extra field.
- Step 4: after a full laptop → server sync of the fixtures, no marker string
  appears anywhere in the server's SQLite file.
- Step 7: with a mock model that echoes its prompt, no marker appears in any
  server-side model prompt. `tests/test_stage_b.py` runs two laptops whose
  fake Observer quotes their raw turns (secrets, paths and emails among them)
  and whose homes hold a file the Gardener may not edit and one over the
  snapshot cap; with both laptops asleep the server reflects, and no marker
  is in the Synthesizer's prompt, in anything the Gardener was sent, or in
  the server's SQLite file.

---

## 5. The Gardener

### 5.1 Scope

The Gardener tends the instructions your coding agents read. It may propose
changes to:

- `CLAUDE.md` files
- `AGENTS.md` files
- skills (files inside a skill directory, such as `.claude/skills/<name>/` or
  `.agents/skills/<name>/`)

Nothing else. **No repo code**, no settings files, no MCP config. The allow-list
is a path check in the **applier** on the laptop, not a line in a prompt, so a
proposal that names any other path is refused even after approval.

Every change lands as a **git branch** that the human approves. wisen never
pushes to, or merges into, a default branch.

**What is worth proposing.** Decided by the owner on 2026-10-28, after a
batch of proposals that were each rejected for one of these reasons. The
Gardener's instructions carry all of them; the draft check in
`wisen/gardener/agent.py` enforces the ones the code can see.

| Rule | In the prompt | Enforced in code |
|---|---|---|
| Recurring: the problem shows on at least two different days | yes | each evidence quote names its reflection date; fewer than two distinct dates, or a date the prompt did not show, is sent back; a day with only one reflection makes no run |
| General: guidance across cases, not a one-situation procedure | yes | — |
| Ownership: personal workflow goes in the owner's own files, never a shared team repo; a team repo gets only what every contributor needs | yes | — |
| Load frequency: every-turn rules in a `CLAUDE.md`/`AGENTS.md` at the narrowest scope that covers them; task-only rules in a skill | yes | — |
| Harness match: a rule about one harness's behaviour goes in that harness's own instruction file | yes | — |
| Not about wisen: friction in wisen's own pipeline is a wisen bug, not an agent-file edit | yes | — |
| Not already handled: compare with past proposals' content, even at another file; applied ones stand unless the problem recurs after the merge; rejected ones are dropped or moved where the note points | yes, through the `past_proposals` tool (content, note, merge date, verdict) | the same change to the same file is refused |

### 5.2 The loop

propose → approve → apply as a branch → measure weeks later.

```mermaid
sequenceDiagram
    autonumber
    participant G as Gardener (laptop in A, server in B)
    participant S as Server mailbox
    participant H as Human (web app or Telegram)
    participant L as Laptop applier

    G->>G: read recent reflections, friction, current file
    G->>G: replay eval: proposed file vs current,<br/>plus the clean set (capped)
    G->>S: ProposalPacket (diff + rationale + evidence + replay result)
    S->>H: push with the morning briefing: "2 proposals waiting"
    H->>S: approve / reject (with a note) / let it expire
    S->>L: decision (laptop polls)
    L->>L: re-check: approval record exists, path is allowed,<br/>file unchanged since the diff was made
    L->>L: create branch wisen/gardener/{proposal id}, commit
    L->>S: ProposalUpdatePacket: applied, branch name
    S->>H: the card shows the branch's diff and "Push & create PR"
    H->>S: POST /api/proposals/{id}/publish (202, "publishing")
    S->>L: device job: publish (laptop polls)
    L->>L: re-check: approval record, branch is the applied one;<br/>git push the one branch; gh pr create (or find the open PR)
    L->>S: JobStatusPacket with the pull request's link
    Note over H,L: the human merges the pull request (or the branch) when they choose
    L->>S: ProposalUpdatePacket: merged (detected locally)
    Note over S: measure timer set for 2 weeks after merge
    S->>L: device job: measure proposal
    L->>L: eval loop: field metrics before vs after (§5.4)
    L->>S: ProposalUpdatePacket: kept / revert_proposed, with the numbers
    G->>S: if worse: a new proposal to revert
```

How pydantic-ai fits:

- The Gardener's `propose_change` tool is declared with `requires_approval=True`.
  The run ends with `DeferredToolRequests`. wisen stores the run's
  `Conversation` (with its pending call) in SQLite under the proposal id.
- When the decision arrives, a new run resumes from that stored `Conversation`
  with `DeferredToolResults`: `ToolApproved` or `ToolDenied(message=…)`. A
  rejection note reaches the model, so the next proposal can learn from it.
- Applying is a separate step on the laptop. In stage B the server-side tool
  raises `CallDeferred`, and the laptop's result comes back the same way.
- **Approval is not authorization.** pydantic-ai's own docs say its approval
  flow does not protect against an untrusted client. So the server
  authenticates whoever approves, and the laptop re-checks the stored decision
  and the path allow-list before it writes anything.

Publishing (steps 10 to 13) is the owner's second tap. The server cannot
push to GitHub and holds no token for it, so the tap only queues a `publish`
device job (request id `publish:<id>`, one per proposal; a second tap while
it runs changes nothing, a tap after a failure runs it again). The laptop
(`wisen/gardener/publisher.py`) re-checks from its own records that the
approval exists and the branch still sits at the commit the applier made,
pushes that one branch to the repo's remote with an explicit refspec (never
the default branch, never force), and opens the pull request with the `gh`
CLI, titled like the commit and carrying the rationale, evidence and replay
summary. A rerun finds the branch's open pull request instead of opening
another. The link comes back in the job status and is recorded on the
proposal (`proposals.pr_url`); the state stays `applied`. Merge detection
asks gh about a published proposal's pull request, whatever branch it was
based on, and ignores the local branch: merged, closed unmerged
(`abandoned`), or still open (no answer from gh: nothing moves that pass). A failure reaches the card as an error class
only (`NoRemote`, `NoGhAuth`, `PushFailed`, `PrFailed`, `NotApplied`); the
message, which may name paths, stays in the laptop's log. `home` targets
have no remote, so the dashboard shows no button for them and the server
refuses the call: their branch stays local for the owner to merge.

> 中文说明：pydantic-ai 的「延迟工具」负责「暂停」：Gardener 想改文件时，本次运行直接结束并保存对话；人可能几小时后在手机上批准，然后用保存的对话 + 审批结果开一个新运行接着走。但「谁有权批准」和「改哪些路径」不能交给模型或这个协议，必须由服务器鉴权、由笔记本上的 applier 再检查一次。

### 5.3 Measuring

Measurement reuses the eval-loop plan (`docs/plan/eval-loop.md`): Tier-1
friction metrics per day, friction half-life, and replay of mined correction
cases where they exist. Two changes to that plan:

- Replay runs **before** a proposal is sent, when cases exist, and its result
  goes into the proposal as evidence. It never applies a change by itself
  (this replaces the automatic accept/revert in eval-loop Phase 3).
- After merge, a timer fires a measure job 2 weeks later. A "worse" result
  makes the Gardener propose a revert, which goes through the same approval.

Measurement reads transcripts, so it always runs on the laptop. What is
measured, and what counts as better, is the
[verification loop](#54-verification-loop-climbing-the-hill).

### 5.4 Verification loop: climbing the hill

**What it is.** The hill is "my coding agents solve my problems with less of
my time, less money and less friction". Each approved Gardener proposal is one
step up it. The verification loop is how wisen knows a step went up and not
down: a controlled check before you approve, and a field check two weeks after
you merge. A step that went down is reverted, and the reason is written down
so the next proposal learns from it.

> 中文说明：「爬山」＝ 让编码 agent 用更少的人力、更少的钱、更少的摩擦解决问题。每个被批准的 Gardener 提案是往上走的一步。验证循环负责回答「这一步是上坡还是下坡」：批准前先用回放评测做对照实验，合并两周后再看真实数据；变差就提议回滚，并把原因记下来留给下一次提案。

**The metric layers.**

| Layer | Metric | Better is | Source |
|---|---|---|---|
| 1. Outcome | **Problems solved** per day ([definition below](#problems-solved-definition)), with a quality guard: the share of solved problems later reverted or re-fixed | more solved, smaller guard share | local git repos of the projects the sessions ran in |
| 2. Cost | **Tokens per problem solved**, $ per problem solved (only where the transcript records cost), user turns per problem solved, and **human-in-the-loop turns per PR**: the user turns spent across the sessions that led to one PR, from the first related session until the PR merged | lower | transcripts (token usage per assistant message) |
| 3. Friction | **Corrections per 100 user turns**, by detector (tool rejection, interruption, explicit correction, rephrase storm); **friction half-life** (mean days a friction persisted before it resolved) | lower | transcripts (`wisen/evals/miner.py`), reflections (`friction_deltas`) |
| 4. Controlled check | **Replay pass rate** per config version on the confirmed eval cases, plus the **clean set** (smooth moments a change must not break) | eval up, clean not down | `wisen/evals/replay.py`, `wisen/evals/runner.py` |
| Trust check | **Alignment correlation**: Pearson r between the reflection's `alignment_score` and the measured correction rate; it should be negative | below zero | `wisen/evals/metrics.py` |

Tokens per problem solved and human-in-the-loop turns per PR are the two
headline cost numbers.

<a id="problems-solved-definition"></a>
**Problems solved: the definition, confirmed by the owner on 2026-10-22.** A
problem is solved when the work of a coding session lands:

- Only the owner's commits count: a commit's author email must match the
  owner's identity (`[gardener].owner_email` in the wisen config, else the
  repo's `git config user.email`). For a merge commit, what counts is who
  wrote the commits it brought in, not who clicked merge. A teammate's PR
  merged during your session, even by you, is not your solved problem.
- Each landed commit counts once, for one problem at most.
- Sessions on a feature branch belong to that branch. The branch is one
  problem (one "PR"). It is solved when it reaches the project's default
  branch: a merge commit on the default branch names it, or its tip is
  contained in the default branch; at least one commit it landed must be the
  owner's. It counts on the day it landed.
- A session on the default branch itself is one problem. It is solved when one
  of the owner's commits lands on the default branch between the session's
  start and one hour after its end. A commit inside several sessions' windows
  (say a Q&A session and a working session open at once in one repo) is
  credited to the nearest one only: the session it is closest to the end of,
  then the one that started latest.
- Quality guard: a solved problem is **reverted** when a later default-branch
  commit says `This reverts commit <sha>` for one of its commits, and
  **re-fixed** when, within 7 days, a default-branch commit whose subject
  starts with "fix" touches a file it touched.
- Human-in-the-loop turns per PR: the user turns of all sessions on that
  branch, from the first one until it landed.

The data comes from the local clones (the default branch and its
`origin/` copy, as of the last fetch). Squash merges that do not name the
branch are missed until the GitHub connector supplies merged-PR data. The
definition lives in one function (`problems_solved` in
`wisen/evals/verification.py`), so changing it changes nothing else.

**Before you approve: replay.** When the Gardener drafts a proposal and
confirmed cases exist for the file it changes, wisen replays them under the
current file and under the proposed one, plus the clean set, through the
existing runner interface. At most `[gardener].replay_cases` cases of each kind
run per proposal (default 5; 0 turns replay off), and the tokens and dollars
each replay spent are recorded. The result goes on the proposal, so you see the
expected gain before you approve. The **accept rule**: approve only if the
replay pass rate rises and the clean-set pass rate does not drop. wisen shows
whether a proposal meets it; you still decide. A proposal with no cases says
so. The proposal also shows the two headline cost numbers as they stand (7-day
medians of tokens per problem solved and human-in-the-loop turns per PR), in
the dashboard and in `wisen gardener list`.

**The clean set.** `wisen eval mine` saves one clean case (kind
`clean`) from each session with no detection: its last user turn, with the
turns before it as context. Its behavior check is fixed (did the replay do
something the user would have had to correct, stop or undo?), so it needs no
model to label it. Like a correction case, it joins the replay only once you
confirm it with `wisen eval review <id> --confirm`.

**After you merge: the field check.** Two weeks after merge, the measure job
compares 7-day rolling medians: the 7 days before the merge against the last 7
of the 14 days after it (the first week lets the change settle).

| Metric | Helped when | Worse when |
|---|---|---|
| Corrections per 100 user turns | down by more than 5% | up by more than 5% |
| Friction half-life | down | — |
| Tokens per problem solved | down or flat (within 5%) | up by more than 10% |
| Human-in-the-loop turns per PR | shown in the reason, not judged | — |

Helped: corrections down, friction half-life down (or not measurable in either
window), and tokens per problem solved down or flat. Worse: corrections or
tokens per problem solved worse. Anything else, including too little data, is
no change. Helped or no change keeps the change (`kept`);
worse moves the proposal to `revert_proposed` and drafts the revert as a new
proposal, which you approve like any other. The verdict, the numbers and the
reason are stored with the proposal, and the next Gardener run reads them, so
"kept: corrections 4.1 → 2.9 per 100 turns" or "reverted: tokens per problem
solved rose 18%" shapes what it proposes next.

**On the dashboard: the hill.** A timeline of corrections per 100 user turns
and the two headline costs, tokens per problem solved and human-in-the-loop
turns per PR, per day, with a marker at each merged Gardener change, labelled
with its file and verdict. A climb shows as the lines stepping down after
markers.

### 5.5 Step 5 decisions in practice

Decided by the owner on 2026-10-22 (open questions 6, 10 and 11):

- **Files outside a git repo** (`~/.claude/CLAUDE.md`, personal skills): wisen
  keeps its own git repo for them at `~/.wisen/gardener/home.git`, with your
  home directory as its work tree. These files are named `home:<path relative
  to home>`, for example `home:.claude/CLAUDE.md`. Its `main` branch records
  the files as they are on disk; wisen commits there only to record a file as
  it found it, never a change of its own. A proposal lands on
  `wisen/gardener/<id>` like any other, and you merge it with
  `git --git-dir ~/.wisen/gardener/home.git merge wisen/gardener/<id>`, which
  writes the file. No setup is needed.
- **Timing:** a proposal expires 7 days after it is made if nobody decides
  it. Its effect is measured 2 weeks after merge.
- **Batching:** the Gardener runs once a day, right after the morning
  reflection, and its proposals arrive as one batch with the morning briefing
  (one push: "Reflection ready · 2 proposals waiting"). You approve or reject
  each item on its own.
- **A day with nothing new is skipped** (owner, 2026-10-28: "no run if no
  history"). `synthesize_day` in `wisen/synthesis/job.py`, which the CLI, the
  launchd script and the server's device job all go through, leaves a day
  alone when it has no sessions, or when its reflection was already made
  after its last session ended (so the laptop's and the server's schedules
  asking for the same day make one reflection). No reflection, no Gardener
  run. The skip is returned, printed, and reported on the device job
  (`skipped: no new sessions`), never silent; `wisen synthesize <date>
  --force`, or the dashboard's Trigger, runs the day anyway. The check reads
  only local agent history, never git or the GitHub API. Upgrade the server
  before (or with) the laptops: the device job status gains `skipped`, which
  an older server's packet check refuses. wisen's own `claude -p` synthesis runs are
  never ingested as sessions (they run in `~/.wisen/claude-cli`, which the
  Claude Code connector skips), so wisen does not reflect on its own
  reflection runs.

How the applier writes a branch without touching your checkout: it builds the
commit in a temporary index (`git apply --cached`, `commit-tree`) and creates
the branch ref only if it does not exist. The commit's author, time and parent
are fixed by the approval, so a rerun after a crash or a reconnect makes the
same commit, and the branch is never created twice.

---

## 6. State: jobs and proposals in SQLite

The owner's decision: a "hand-built state machine in SQLite for proposals and
jobs", with the durable job engine built from scratch ("Build the job engine
myself"), and DBOS as the fallback.

### 6.1 Proposal state machine

```mermaid
stateDiagram-v2
    [*] --> proposed: Gardener run pauses for approval
    proposed --> approved: human approves
    proposed --> rejected: human rejects (note goes to the model)
    proposed --> expired: no decision before expires_at
    proposed --> superseded: a newer proposal for the same file
    approved --> applied: laptop commits the branch
    approved --> failed: file changed, path refused, git error
    applied --> merged: branch reached the default branch (or its PR merged)
    applied --> abandoned: branch deleted (or its PR closed) unmerged
    merged --> measuring: N weeks after merge
    measuring --> kept: helped or no change
    measuring --> revert_proposed: worse
    revert_proposed --> [*]: a new proposal (the revert) starts its own life
    rejected --> [*]
    expired --> [*]
    superseded --> [*]
    failed --> [*]
    abandoned --> [*]
    kept --> [*]
```

Rules:

- **One function moves a proposal**: `transition(proposal_id, from_state,
  to_state, request_id, actor)`. It runs a compare-and-set
  (`UPDATE … WHERE id = ? AND state = ?`) and appends a row to an audit table in
  the same transaction. An illegal or stale transition changes nothing.
- **Decisions are idempotent.** Every decision carries a `request_id`. The same
  approval arriving twice (a Telegram retry, or a tap on both the web app and
  Telegram) returns the first result. The second channel shows "already
  decided".
- **The laptop is the authority for the git states** (`applied`, `merged`,
  `abandoned`) because only it can see the repo. The server is the authority
  for the decision states.
- **Proposals made before pairing upload with their history.** On the first
  sync after pairing the laptop sends each proposal the server lacks, in any
  state, carrying the decision or expiry it got while unpaired, then its git
  moves in order. The server records that decision through its normal decision
  record, as of the time it was made (so one made before the deadline still
  counts), only as part of the proposal's first upload: once the server holds
  a proposal, no device packet decides it. A proposal the egress gate refuses
  stays local, with its moves; the reason is logged once.

### 6.2 The job engine

One small engine, the same code on the laptop and the server, each with its own
SQLite file. It replaces the in-memory `_jobs` dict in `wisen/main.py`.

| Concept | What it is |
|---|---|
| **Job** | A row: `id`, `user_id`, `kind`, `args` (JSON), `state`, `attempts`, `max_attempts`, `run_after`, `request_id` (unique per user; the row is the request's receipt). States: `queued → leased → succeeded / failed`, plus `waiting` for a parked job; a failed attempt goes back to `queued` with backoff until `max_attempts`, then `failed`. A resent request returns the job as it is; an explicit rerun (`rerun=True`, used for `daily-synthesis:{date}` by the CLI, launchd and `POST /synthesize`) resumes an unfinished job, re-queues a failed one from its last checkpoint, and starts a succeeded one over, each with fresh attempts. (`parent_id`, for sub-jobs, is not built yet.) |
| **Step** | A named, memoized unit inside a job. Its output is saved on success. When a job reruns after a crash, finished steps return their saved output instead of running again. Outputs are dropped once the job succeeds; a failed job keeps them so a rerun resumes from them. |
| **Lease** | A claim on a job with an expiry. A worker heartbeats to extend it. On startup, and on every scheduler tick, expired leases go back to `queued`; one that expires on the job's last attempt fails the job. |
| **Timer** | A job with `run_after` in the future. The scheduler sleeps until the earliest one. Wake-ups may repeat, so handlers must be idempotent. |
| **Wait** | A job parked until an event arrives (a decision, a laptop result), with an optional timeout. |
| **Replay policy** | Each step declares whether rerunning it is safe. Reads and model calls are safe to rerun. Side effects (create a branch, send a push) are not: if a crash interrupts one, the job fails with `StepInterrupted` for a human to check instead of running it again. |

**Two kinds of worker, one queue.** On the server, a job's `runner` column says
who may claim it: the server itself, or a device. The server runs its own jobs
in-process. Device jobs (synthesize a day, apply a proposal, answer a raw
search, measure) are claimed by the laptop over HTTPS with the same lease
rules. A laptop that sleeps mid-job just lets the lease expire, and the job
waits for it to come back.

> 中文说明：「租约」（lease）＝领任务时拿到一张有到期时间的“借条”。干活的进程要定期续期；如果它崩了或笔记本睡着了，借条过期，任务自动回到队列等别人（或醒来的自己）再领。配合「每一步完成就存结果」，重跑时已完成的步骤直接取存档，不会重复调用模型或重复建分支。

**Where pydantic-ai fits.** pydantic-ai runs each agent call. The engine
treats one agent run as one step. A later, optional move plugs the engine in
as a real pydantic-ai durable backend through its public backend builder
(`JournalCallableOperationBackend`), so model and tool calls inside a run are
journaled too. pydantic-graph is not used for state: its persistence was
removed in V2.

**The DBOS fallback.** If the hand-built engine cannot pass the crash tests in
step 3, or keeps costing more time than the features built on it, swap in DBOS
(in-process, SQLite, `DBOSDurability`) behind the same job interface. The
proposal state machine stays hand-built either way.

---

## 7. Storage

**SQLite everywhere; MongoDB is retired.** One file per side:

- laptop: `~/.wisen/wisen.db`
- server: `/var/lib/wisen/wisen.db`

Both use WAL mode. Schema changes are numbered SQL migrations, tracked in a
`schema_version` table.

| Table (laptop) | Replaces | Notes |
|---|---|---|
| `reflections` | Mongo `reflections` | One row per `(user_id, date)`. The full `Reflection` as JSON, plus a few indexed columns. |
| `conversations` | Mongo `chat_sessions` | The pydantic-ai `Conversation` as JSON, so a resumed chat keeps its tool calls and results. |
| `voice_sessions` | Mongo `voice_sessions` | Same fields as today. |
| `jobs`, `job_steps` | `_jobs` dict | The job engine. |
| `proposals`, `proposal_events` | — | The proposal state machine and its audit log. `proposals.pr_url` is the pull request opened for an applied branch (the laptop records it when it opens it; the server, from the publish job's status). |
| `egress_ledger` | — | Every packet sent. |
| `gardener_runs` | — | A paused Gardener run: its `Conversation` (with the pending calls), the day it ran for, and its final answer once resumed. |
| `proposal_git` | — | Where an applied proposal lives: repo, branch, base and branch commits, the resulting file blob, when it merged. |
| `proposal_measures` | — | The field check's verdict, reason and numbers per proposal. |
| `daily_metrics` | — | One row per day of verification-loop metrics ([§5.4](#54-verification-loop-climbing-the-hill)). |

The server has the same job and proposal tables, plus `users`,
`web_sessions`, `pairing_codes`, `devices` (one token per device, stored as a
hash on the device row; re-pairing makes a new device), `push_subscriptions`,
`telegram_links`, and `packets` (received packets, by type).

**Every row carries `user_id`.** On a laptop it is a fixed local id. After
pairing, the packets the laptop sends carry the server's user id; local rows
keep the local id.

Moving existing data: a one-time `wisen import-mongo` command reads the old
MongoDB and writes SQLite. After step 1, MongoDB and `motor` are no longer
needed, and the quickstart drops `brew services start mongodb-community`.

---

## 8. The server

### 8.1 On the owner's Hetzner box

The box: Ubuntu, 2 vCPU, 3.7 GiB RAM (about 1.7 GiB free, no swap), no Docker,
Caddy already serving HTTPS, and the wisen dashboard prototype already running
on port 8722. That rules out a MongoDB server, Temporal, and a local model, and
it favours one small process.

```
Caddy (existing, :443, automatic HTTPS)
  └─ wisen.vela-server.duckdns.org → 127.0.0.1:8722
systemd: wisen.service          one process: `wisen cloud serve`, uvicorn, 1 worker
  ├─ dashboard + approval inbox (installable web app, service worker)
  ├─ device API (pairing, packets, job claim/heartbeat/result, decisions)
  ├─ job engine + scheduler (asyncio task in the same process)
  ├─ Web Push sender (VAPID keys)
  ├─ Telegram bot (long polling)
  ├─ stage B only: Synthesizer (`claude -p`, API fallback), Gardener and
  │                reflection chat (pydantic-ai, API key)
  └─ /var/lib/wisen/wisen.db (SQLite, WAL)
systemd: wisen-backup.timer     daily, Persistent=true → consistent SQLite snapshot, copied off the box
```

- **One process owns the database and the timers.** No message broker, no
  separate worker fleet on the server: the laptop is the worker.
- It **replaces the dashboard prototype** on port 8722 in place, and binds to
  `127.0.0.1` only. Caddy gets one site block for the hostname.
- The unit runs as a dedicated user with `MemoryMax=` set, so it cannot starve
  the other apps on the box, plus the usual systemd sandboxing
  (`NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/wisen`).
- **Secrets.** Stage A: VAPID keys and the Telegram bot token, in a
  root-owned `EnvironmentFile`. **No model key.** Device tokens and login
  sessions are random values stored only as hashes in the database, so there
  is no session secret.
  Stage B adds the model provider key to the same file, for the Gardener and
  chat. Synthesis runs on `claude -p` on the owner's subscription (open
  question 9, decided), logged in for the service's user; the API model is
  the fallback by config. One `claude -p` process peaked at about 236 MiB on
  the box (measured 2026-10-28, before its model call: the box's login had
  expired on 2026-09-15; a full call is re-measured once it is logged in
  again), so the units set `MemoryMax=800M` (deploy/README.md).
- **Backups** hold packets and results only, never raw transcripts, so off-box
  backup is low-risk. That matters: Claude Code deletes local transcripts after
  30 days by default, so the server's copy becomes the long-term record. Test a
  restore once.

### 8.2 Devices and tokens

- **Pairing.** In the logged-in dashboard, "Add device" shows a one-time code.
  On the laptop, `wisen cloud pair https://<host>` exchanges it for a random
  device token. The server stores only a hash.
- **Use.** The laptop sends `Authorization: Bearer <token>` over TLS. A token is
  scoped to one user and one device.
- **Revoke.** The dashboard lists devices; revoking one makes its next call fail
  with 401.
- **Outbound only.** The laptop daemon (`wisen worker`, kept alive by launchd)
  polls the server for device jobs and decisions. Long-polling first, a
  WebSocket later if needed. The laptop never opens a port.
- **Browser login.** The shared Basic-auth password in
  `wisen/dashboard/server.py` is replaced by a login and a session cookie.

---

## 9. Notifications, approvals and Telegram

### 9.1 Web Push

The dashboard becomes an installable web app (manifest + service worker). It
subscribes to Web Push, and the server sends notifications with `pywebpush`
and VAPID keys. Web Push payloads are end-to-end encrypted between the server
and the browser. On iPhone, Web Push works only after the app is added to the
Home Screen. A notification opens the proposal in the approval inbox.

### 9.2 Telegram

Telegram is a second channel for the same things ("also add support in
telegram"):

- **Linking.** The dashboard shows a one-time code; you send it to the bot.
  The server stores the chat id against your `user_id`. Messages from unlinked
  chats are ignored.
- **Notifications.** New proposals, a finished morning reflection, a measure
  result.
- **Approval buttons.** Approve / Reject buttons on each proposal (inline
  keyboard). A tap becomes a decision with a `request_id`, through the same
  `transition()` as the web app, so both channels stay consistent.
- **History chat** ("can i also add a chatbot where i can search history in
  telegram?"):
  - **By default, it answers from reflections.** In stage A the question is
    queued as a device job, and the laptop's chat agent answers with
    reflection tools only. In stage B the server answers directly.
  - **Raw-history search only on explicit request** (for example a `/search`
    command). It is always a device job, answered by the laptop with the
    existing grep-first search (`wisen/chat/search.py`). What comes back is an
    `AnswerPacket` (a written answer with short quotes), never raw hits.
  - If the laptop is asleep, the bot replies that the question is queued, and
    answers when the laptop wakes.

The server reaches Telegram with long polling, so the bot needs no extra public
endpoint. Telegram stores bot chats on its own servers (they are not end-to-end
encrypted), which is why how much detail a Telegram message carries is listed
as open question 8.

---

## 10. Self-hosting and multi-user readiness

The owner: "each self-hosts for now", with a possible hosted service later.

- **Each developer runs their own server**, with their own keys. wisen never
  holds another person's data.
- **Nothing Hetzner-specific in the app.** Config is environment-only (data
  dir, public URL, keys). The repo ships samples in `deploy/`: a systemd unit,
  a backup timer and a Caddyfile snippet.
- **Multi-user-ready, single-user-run.** Every row has `user_id`; every token,
  push subscription and Telegram link is scoped to one user; no query reads
  across users. A hosted service would still need real per-tenant work
  (encryption at rest, API keys per tenant, never subscription credentials),
  and it is not part of this plan.
- The device protocol is plain JSON over HTTPS, so another server
  implementation could replace this one later.

---

## 11. pydantic-ai: what is used, what is built

| Piece | Use from pydantic-ai | Build in wisen |
|---|---|---|
| Agent calls | `Agent`, structured output, tools | — |
| Approval pause | `requires_approval=True`, `DeferredToolRequests` / `DeferredToolResults`, `ToolApproved` / `ToolDenied` | Approval inbox, push, Telegram, authentication, the proposal state machine |
| Work on another machine | `CallDeferred` (stage B Gardener → laptop applier) | Device jobs, leases, the applier |
| Saving a paused run or a chat | `Conversation` + `ConversationTypeAdapter`, stored as JSON in SQLite | — |
| Durable jobs | Later and optional: the durable-backend builder, to journal inside a run | The job engine (fallback: `DBOSDurability`) |
| Multi-agent | Plain programmatic hand-off (Observer → Synthesizer → Gardener) | Which agent runs on which machine, and the packet hand-off between them |
| Telemetry | `InstrumentationSettings(include_content=False)` when traces leave the laptop | — |
| Graph state | Not used (pydantic-graph persistence was removed in V2) | — |

---

## 12. Build order

Each step leaves a working wisen. Local-only mode keeps working after every
step.

| Step | Delivers | Verified by |
|---|---|---|
| **0. This design doc** | `docs/plan/cloud-architecture.md` with the system, approval-flow and state-machine diagrams | Diagrams render on GitHub; owner review |
| **1. SQLite on the laptop** | A storage module replacing `wisen/db.py` and every Mongo call site; chats stored as `Conversation`s; `wisen import-mongo`; `motor` removed; README and CLAUDE.md run instructions without MongoDB | All tests pass with no MongoDB running; importing a real Mongo database gives the same reflection count; a resumed chat still sees its earlier tool results |
| **2. Packet boundary** | Shared packet models (starting from `CloudReflection`); the packet envelope (`packet_type`, `schema_version`, `user_id`, `packet_id`, `created_at`); the egress gate (allow-list → redact → truncate → validate → ledger → send); `wisen egress` | `tests/test_egress.py` extended: a fixture with a fake API key and an absolute path yields a packet with neither; a packet with an extra field fails validation; a proposal whose diff is over its cap or contains a secret is rejected, not cut or rewritten |
| **3. Job engine + proposal state machine** | `jobs`/`job_steps` with leases, timers, waits and replay policies; `transition()` with audit; `_jobs` in `wisen/main.py` and the launchd synthesis run moved onto it | Kill the process between Observer and Synthesizer, restart: the Observer is not called again and exactly one reflection is stored; a duplicate `request_id` returns the existing job; an illegal transition changes nothing; an expired lease is requeued on startup |
| **4. Server, stage A** | `wisen cloud serve` replacing `wisen/dashboard/server.py`; login, pairing, device tokens; the mailbox and device-job API; approval inbox; installable web app with Web Push; `device_id` in the packet envelope; `wisen worker` on the laptop; `deploy/` samples; backup timer | On the Hetzner box: pair the laptop; a day's reflection arrives and the phone gets a push; a revoked token gets 401; a server restart mid-lease recovers the job; a restored backup serves the same data; the server's environment has no model key; no fixture marker string appears in the server database |
| **5. Gardener loop** | The Gardener agent and its `requires_approval` tool; proposals from reflections; the laptop applier (allow-list, branch `wisen/gardener/<id>`); merge detection; the measure timer wired to the eval loop ([verification loop](#54-verification-loop-climbing-the-hill); decisions in [§5.5](#55-step-5-decisions-in-practice)) | An approved proposal produces exactly one commit on one branch even if the laptop reconnects mid-apply; a proposal for a disallowed path is refused after approval; a rejection note reaches the next run; a measure job fires after merge and records its result |
| **6. Telegram** | Bot linking, notifications, approve/reject buttons, history chat (reflections by default, `/search` on the laptop) | Approving in Telegram and then in the web app records one decision; an unlinked chat is ignored; `/search` while the laptop sleeps is answered after it wakes |
| **7. Stage B** | `ObservationPacket` and `ConfigSnapshotPacket` upload; Synthesizer, Gardener and reflection chat on the server over packets, one reflection per day from every laptop; model key (or `claude -p` for synthesis) on the server; applying still on the laptop | With the laptop asleep, the morning reflection is produced from packets already uploaded; with a mock model that echoes its prompt, no fixture marker appears in any server-side prompt or row |

---

## 13. Out of scope

- The Gardener editing repo code, settings files or MCP config.
- Merging branches. wisen creates them; you merge them.
- Hosting other developers' data on the owner's server.
- A model running on the Hetzner box (no GPU, little RAM).

---

## 14. Open questions

The research raised these and the owner has not decided them. Each names the
step that needs the answer. Numbers stay fixed so other sections can cite
them; questions the owner has since decided are listed under
[Decided](#decided) instead.

1. **Tailscale or public HTTPS for the device API?** The design works with
   public HTTPS plus device tokens. Tailscale would hide the device API from the
   internet, but every device (including a phone, if approvals go over the
   tailnet) needs the client. *Needed by step 4.*
2. **Hostname.** `wisen.vela-server.duckdns.org` needs to resolve (DuckDNS
   sub-subdomains were not verified). Fallback: a path on
   `vela-server.duckdns.org` with tightly scoped cookies, which shares an origin
   with the other apps on the box. *Step 4.*
3. **A local-model Observer?** An on-laptop model (for example through
   pydantic-ai's Ollama provider) would keep raw text away from every provider.
   Quality is unknown until measured with `wisen eval`. Should it be offered,
   and should it ever be the default? *Any time after step 2.*
5. **Vendor clouds as hands.** May an approved proposal be carried out by a
   vendor's cloud agent (Claude routines, Codex cloud), given those vendors would
   see and store the proposal? Not planned; listed so it is decided on purpose.
8. **How much a Telegram message carries.** Title and a link to the web app, or
   the full proposal and diff? Telegram stores bot chats on its servers. *Step 6.*
12. **When to make the Observer's per-turn cap configurable.** The owner
    accepts excerpts up to about 4,000 characters per turn, but no build step
    delivers the setting yet. Which step should, and what should the default be
    once `wisen eval` has compared the sizes?

### Decided

Decided by the owner on 2026-10-22, for step 5. How the build carries them
out is in [§5.5](#55-step-5-decisions-in-practice).

6. **Files outside a git repo.** `~/.claude/CLAUDE.md` and personal skills
   often are not in a repo. *Decided:* wisen keeps its own local git repo for
   them, so the one-branch-per-proposal, owner-merges flow works with no
   setup. (The other options were to require a dotfiles repo, or to skip
   these files.)
10. **Timing.** *Decided:* a proposal expires after 7 days unanswered; its
    effect is measured 2 weeks after merge.
11. **Proposal batching.** *Decided:* a daily batch that arrives with the
    morning briefing, approved item by item. (The other option was one
    approval request per proposal.)

Decided by the owner on 2026-10-22, for step 7. How the build carries them
out is in [§3.2](#32-stage-b-nrem-on-the-laptop-rem-on-the-server).

4. **Several machines.** *Decided:* the server merges one day's packets from
   all laptops into **one** reflection. (The other option was one reflection
   per machine.) The day's metrics row is still one per day, from the laptop
   that sent it last; merging the numbers of several laptops is not built.
7. **What the stage B Gardener may see.** *Decided:* the file text, as a
   redacted `ConfigSnapshotPacket` the laptop uploads (current text of
   allow-listed files only, secret-redacted). (The other option was that the
   server writes only the intent and the laptop drafts the diff, at the cost
   of a laptop model call.)
9. **`claude -p` on the server.** *Decided:* run it on the server (the
   owner's own box, the owner's own use), measure the CLI's memory there, and
   keep the API-key path as the fallback. Synthesis runs on `claude -p` by
   default and on the API model with `WISEN_SYNTH_MODEL=api`; the Gardener
   and chat use tools, so they stay on the API model. Measured: about 236 MiB
   per `claude -p` process (§8.1).
