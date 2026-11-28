# Find five replay tasks in your coding history

Paste the prompt below into [Wisen chat](chat.md). When you try a case, give a
fresh agent only its replay prompt, not the grader notes. Keep the results
private: they may contain details from your sessions.

```text
Search my coding-agent conversations from the last 30 days. Find five distinct
times I corrected an agent that would make good replay tasks. The question is:
could a fresh agent have gotten this right using only what it could have known
before the mistake?

Use daily reflections as leads, then verify each case in the original
conversation. Look for requests to undo or redo work, not just phrases like
“no” or “use X instead.” Keep a case only if you can point to a clue that was
already available and describe how to tell success from failure. Skip cases
where I changed my mind, added a requirement later, or where you can't verify
the original exchange. Prefer recurring mistakes, but pick only one example
of each kind. Return fewer than five rather than guess.

Give me two separate sections, matched by neutral IDs (A–E):

1. Replay prompts. Label each “Paste into a fresh chat.” Include the original
   request and the minimum context a fresh agent needs. Don't include the
   mistaken response, my later correction, a revealing title, or the scoring
   rule.

2. Grader notes. For each ID, give the date, a safe session reference, what
   went wrong, and a short redacted excerpt of my correction. Point me to two
   places: where the clue was available before the mistake, and where my later
   correction or request for rework confirms it. Use message numbers or
   timestamps if available; don't invent them.

   Write “Pass if…” and “Fail if…” in terms of observable behavior. Say
   whether I can grade the reply alone or must inspect files or run a test.
   If execution is needed, name the starting state and the check. Don't count
   a claim that work was done as proof.

Recommend one case I can try by hand today, say why, and give me one concrete
check I can use to grade it. Search and select only: don't run the tasks, edit
files, or train anything. Omit secrets, private paths, and unnecessary
transcript text.
```
