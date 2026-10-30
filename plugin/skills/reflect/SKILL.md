---
name: reflect
description: Run wisen's daily reflection for a date and summarise it (moods, friction points, the insight, recommendations). Use when the user asks how a day or yesterday went with their agents, or to run or show a wisen reflection.
---

# wisen reflect

One reflection per day, synthesised from that day's agent sessions.

## Run

```
wisen synthesize [YYYY-MM-DD]
```

No date means yesterday. This makes real model calls and can take a minute or two;
tell the user before running it, and run it only once per date per conversation.

## Read

Reflections are exported with:

```
wisen export <dir>
```

Read `<dir>/<date>.json` and summarise in this order, each as one or two sentences:

1. your mood and the agent's mood (label plus summary)
2. the alignment score
3. the insight
4. friction points, one line each
5. recommendations, one line each, with their target

Quote the reflection's own words where it has them. Do not add advice of your own.

## If wisen is not installed

`wisen: command not found` means the CLI is missing. Point the user to `/wisen:setup`.
