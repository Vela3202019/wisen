# Chat

`POST /chat` (SSE) talks to an agent grounded in your latest reflection, with
four tools: fetch any day's reflection, the last 7 days of trends, grep-first
search over your raw session history across all agents, and search over your
harness configs. Ask "how was my week?" and it investigates, reflections first,
then transcripts, then configs, before answering with dates and quotes.

What the chat model receives is listed under
[What leaves your machine](privacy.md).
