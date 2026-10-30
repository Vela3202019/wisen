# Your own server (optional)

wisen runs fully on your laptop. Add a small server of your own and you get
the dashboard on your phone as an installable web app, push notifications
when a reflection or a proposal is ready, an approval inbox, and a daily
schedule that runs while you sleep. Optionally, a Telegram bot sends the same
notifications, approves or rejects proposals with a tap, and answers
questions about your history (your laptop writes the answers, in stage B too). The server is
a mailbox: it never makes a model call, holds no model key, and never sees
raw transcripts. Your laptop does all model work and only ever connects out
to it, with a per-device token you can revoke.

On the server (one Python process; see [deploy/README.md](../deploy/README.md)
for systemd, Caddy and backups):

```bash
pip install "wisen[cloud] @ git+https://github.com/Vela3202019/wisen"
wisen cloud user <name>             # your login (prompts for a password)
wisen cloud vapid-keys              # Web Push keys -> WISEN_VAPID_* env vars
# optional: WISEN_TELEGRAM_BOT_TOKEN from @BotFather (deploy/README.md, "Telegram")
wisen cloud serve                   # 127.0.0.1:8722, behind your HTTPS proxy
```

On the laptop:

```bash
wisen cloud pair https://<host>     # code from the dashboard: Devices -> Add device
wisen worker                        # keep it running with launchd (deploy/launchd/)
```

## Stage B: the server reflects while your laptop sleeps

In stage A the morning reflection waits for an awake laptop. Stage B moves the
Synthesizer and the Gardener to the server, so the reflection is ready when
you wake up, even with the lid closed. Turn it on at both ends, **server
first** (an older server refuses the new packets):

- **Server:** `WISEN_CLOUD_STAGE=b`. Synthesis then runs on `claude -p` on
  your Claude subscription (log the CLI in for the service's user), the
  Gardener on `WISEN_MODEL` with that provider's API key in the server's
  environment file, and chat on DeepSeek (`WISEN_CHAT_MODEL`,
  `DEEPSEEK_API_KEY`); `WISEN_SYNTH_MODEL=api` puts synthesis on the API
  model too. See
  [deploy/README.md](../deploy/README.md#stage-b-the-server-reflects).
- **Each laptop:** `[cloud] stage = "b"` in `~/.wisen/config.toml` (or
  `WISEN_CLOUD_STAGE=b`) and restart `wisen worker`. This is the laptop's
  own opt-in to send more (what exactly is under
  [What leaves your machine](privacy.md)).

The worker then runs the Observer on yesterday and today as sessions come in
(each day at most every 2 hours: each run is one model call on the laptop)
and sends its notes, plus the current text of the files the Gardener may
edit. At the daily time the server asks the laptops for fresh notes, waits up
to 30 minutes for an answer, and then reflects on what it has. With several
laptops, their notes on a day become **one** reflection. Notes that arrive
later (a laptop that slept through the morning) make that day's reflection
again. Applying an approved proposal stays on the laptop whose file it
changes. `POST /api/chat` on the server answers questions from its
reflections (the dashboard does not call it yet).

