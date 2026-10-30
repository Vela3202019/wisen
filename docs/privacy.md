# What leaves your machine

There are three tiers. The numbers are the truncation limits in the code.

**1. Data at rest: local.** Raw transcripts, git history, and agent configs stay
where your agents put them. wisen does not copy them. A local SQLite file
holds reflections (plus chat and voice session records).

**2. Model inference: excerpts go to the model provider you configure.**

- **Synthesis** (`wisen/synthesis/agent.py`): the Observer receives every turn
  of the day's sessions, each turn cut to 400 characters. A turn can include
  tool output (for example Claude Code's `[tool_result]` blocks). It also
  receives up to 20 agent config files (CLAUDE.md, AGENTS.md, MCP configs, …),
  each cut to 1,000 characters, plus the day's git commit messages and
  changed-file lists (via the GitHub API). The Synthesizer then receives the
  Observer's notes, which include short quotes, and earlier reflections.
- **Chat** (`wisen/chat/search.py`): each search returns up to 20 matches. A
  match is the matched turn cut to 500 characters, plus one turn before and
  after it, each cut to 200 characters. Config search returns snippets of up
  to 500 characters. These results, and any reflection the agent fetches, go
  to the chat model.
- **Voice** (`[voice]` extra): your speech goes to Google's Gemini realtime
  model.
- **Stage B server** ([Your own server](server.md#stage-b-the-server-reflects-while-your-laptop-sleeps)): the server's
  Synthesizer, Gardener and chat send the packets your laptop uploaded
  (tier 3) to the server's model providers (chat's to DeepSeek), never
  anything else.

Pick the provider with `model` and `synth_model`. That provider's data terms
apply to these excerpts. If `synth_model` is `claude-cli:…`, synthesis runs
through the Claude Code CLI and sends the same excerpts to Anthropic under
your own Claude account. The terms of that account apply.

**3. Cloud: derived results only, which can include short quotes.** Everything
bound for a server goes through one egress gate (`wisen/egress.py`) as a typed
packet (`wisen/packets.py`). The gate rejects any packet type or field that is
not on the schema, masks secrets (API keys, tokens, private keys), replaces
email addresses and absolute paths, and caps every text field (quotes at 500
characters, briefings at 4,000).

- A paired wisen server receives what `wisen worker` sends: one result
  packet per reflection, cut down to the result fields; Gardener proposals
  (target file, diff, rationale, short quotes; a diff that would need
  redacting or cutting is not sent at all); what became of each approved
  proposal (its branch name, applied / merged / measured, and the measured
  numbers); one row of numbers per day for the verification loop (user turns,
  corrections, tokens, problems solved; no text); and job status (state,
  times, error class, never a traceback). Results' `evidence` fields and summaries
  can quote short pieces of your sessions word for word. Raw transcripts, the
  full turn excerpts, the Observer's notes, the SQLite file, and API keys
  never go to the server, and the server re-checks every packet against the
  same schema and rejects anything else. `wisen export` and
  `scripts/export_reflections.py` write the same result packets to a folder
  instead, for a sync job of your own.
- In **stage B** a laptop that opted in (`[cloud] stage = "b"`) sends two
  more packet types, and stops sending result packets (the server makes the
  reflections). An **observation** packet holds the Observer's notes on one
  day: mood and drift summaries, topics, friction, the day's key moments
  with short quotes (500 characters each, 4,000 for a summary). A
  **config snapshot** packet holds the current text of one file the Gardener
  may edit (a `CLAUDE.md`, an `AGENTS.md` or a skill file, nothing else),
  secret-redacted, with its git blob id; a file over 20,000 characters is
  left out rather than cut, and a deleted file is sent once as gone. Raw
  transcripts, tool output, other files and the SQLite file still never
  leave.
- If you set up the Telegram bot, your server sends Telegram (whose servers
  keep bot chats, not end-to-end encrypted): each proposal's target file, its
  rationale cut to 300 characters and a dashboard link (never its diff or
  evidence quotes), the "reflection ready" line, what became of an applied
  proposal, and the answers to questions you ask the bot. An answer is
  written by your laptop's chat model, sent to the server as an answer packet
  (capped at 4,000 characters and redacted like other packet text), and can
  quote short pieces of your sessions; raw search hits never leave the
  laptop. Questions you type reach your laptop through your server.
- `scripts/collect_usage.py` adds a usage packet with subscription usage
  percentages and reset times. To get the Claude percentages, it reads your
  Claude OAuth token from the Keychain and calls Anthropic's usage endpoint.
  Only the percentages and reset times reach the dashboard, not the token.

Every packet that passes the gate is logged in `~/.wisen/egress.log`: time,
destination, packet type, size, field names and a SHA-256 of the bytes, never
the content. `wisen egress` shows today's (`--date`, `--list` for one line per
packet). Packets sent to your server are logged as `cloud:<host>` just before
they leave; the worker sends a packet again only when its content changed.
The export writes packets to a folder that your own sync job (for example
rsync) uploads, so for those the log records what was prepared for upload; a
packet whose content has not changed is not rewritten or logged again.

## Network calls

Network calls from your machine go to the model providers you configure, the
GitHub API (the git commits connector reads your pushes), Anthropic's usage
endpoint (the optional usage collector), and your own wisen server if you
pair one. Nothing else.
