# Self-hosting the wisen server (stage A)

The server is a mailbox: it stores what your laptop sends (reflections as
result packets, Gardener proposals), holds your decisions, keeps the daily
schedule, and sends Web Push and Telegram notifications. It makes no model calls and holds
no model API key. Raw transcripts never reach it. The laptop only ever
connects out to it, with a per-device token you can revoke.

Everything here is a sample for one Ubuntu box with Caddy already serving
HTTPS. Nothing in the app is specific to that box: it is configured by
environment variables only.

| File | Goes to |
|---|---|
| `wisen.service` | `/etc/systemd/system/wisen.service` |
| `wisen-backup.service`, `wisen-backup.timer` | `/etc/systemd/system/` |
| `wisen.env.example` | `/etc/wisen/wisen.env` (root, mode 0600) |
| `Caddyfile` | a site block in the box's Caddyfile |
| `launchd/com.wisen.worker.plist` | `~/Library/LaunchAgents/` on the laptop |

## Install on the server

```bash
# A dedicated user, and the one directory the service may write
sudo useradd --system --home-dir /var/lib/wisen --shell /usr/sbin/nologin wisen
sudo install -d -o wisen -g wisen -m 0700 /var/lib/wisen

# The code, in its own virtualenv
sudo python3 -m venv /opt/wisen
sudo /opt/wisen/bin/pip install "wisen[cloud] @ git+https://github.com/Vela3202019/wisen"

# Secrets: VAPID keys for Web Push, optionally a Telegram bot token (no model key)
sudo install -d -m 0755 /etc/wisen
sudo install -m 0600 deploy/wisen.env.example /etc/wisen/wisen.env
/opt/wisen/bin/wisen cloud vapid-keys          # paste both lines into /etc/wisen/wisen.env
sudo editor /etc/wisen/wisen.env               # and set WISEN_CLOUD_PUBLIC_URL

# Your login (prompts for a password). The daily synthesis is asked of your
# laptop at --daily-at in --timezone, for the day before.
sudo -u wisen /opt/wisen/bin/wisen cloud user vela --db /var/lib/wisen/wisen.db \
    --timezone Europe/Berlin --daily-at 07:00

# The service and the daily backup
sudo cp deploy/wisen.service deploy/wisen-backup.service deploy/wisen-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wisen.service wisen-backup.timer
```

The service binds `127.0.0.1:8722`. If the old stdlib dashboard prototype
still runs on that port, stop and disable its unit first. Then add the site
block from `Caddyfile` (with your hostname) and `sudo systemctl reload caddy`.

`curl https://<host>/health` should show `"model_key": false`.

To upgrade: `sudo /opt/wisen/bin/pip install -U "wisen[cloud] @ git+…"` and
`sudo systemctl restart wisen`. Schema changes apply themselves on start.

## Pair the laptop

1. Open `https://<host>`, log in, then **Devices → Add device**. It shows a
   one-time code, valid for 10 minutes.
2. On the laptop: `wisen cloud pair https://<host>` and type the code. The
   token is saved in `~/.wisen/cloud.json` (mode 0600); the server keeps only
   its hash.
3. Run `wisen worker` once in a terminal to see it connect, then keep it
   running with launchd: edit the paths in `launchd/com.wisen.worker.plist`,
   copy it to `~/Library/LaunchAgents/`, and
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wisen.worker.plist`.

The worker uploads each day's reflection and each open proposal, long-polls
for work (the daily synthesis, a **Reflect** request from the dashboard, a
**Push & create PR** tap) and for your decisions. Once the server schedules
the daily run, the old launchd one-shot (`scripts/run_synthesis.py`) is no
longer needed on that laptop.

**Push & create PR** runs on the laptop, so it needs the `gh` CLI installed
and logged in there (`gh auth login`), and the repo a proposal changes must
have a git remote. The worker pushes only the proposal's branch
(`wisen/gardener/<id>`, never with force) and opens the pull request against
the repo's default branch; the server never holds a GitHub token. If the
tap fails, the card says why in one line (no remote, gh not logged in, the
push or the pull request refused) and tapping again retries. Personal files
(`home:` targets) have no remote: their branch stays on the laptop for you
to merge.

**Revoke** a device in the dashboard: its next request gets 401 and any job
it held goes back to the queue. The worker then exits and launchd leaves it
stopped until you pair again.

## Notifications

On the phone, open the dashboard and add it to the Home Screen (on iPhone,
Web Push works only from the Home Screen app). Open the installed app and tap
**🔔 Notify me**. A new reflection or a proposal waiting for you then arrives
as a notification that opens it in the app.

## Telegram

The bot sends the same notifications to one Telegram chat, puts **Approve** /
**Reject** buttons on each proposal, and answers questions about your history.
The server long-polls Telegram, so nothing new is exposed on the box. A
proposal message carries its target file, a short description (its rationale,
cut to 300 characters) and a link to the dashboard; never the diff or the
evidence quotes. Telegram keeps bot chats on its own servers, unencrypted, so
the details stay in the dashboard.

Set it up once:

1. In Telegram, open a chat with **@BotFather**, send `/newbot`, and pick a
   name and a username. BotFather replies with a token like
   `123456789:AA…`. Treat it as a password.
2. On the server, put it in the environment file:
   `sudo editor /etc/wisen/wisen.env` and set
   `WISEN_TELEGRAM_BOT_TOKEN=<token>`. Check that `WISEN_CLOUD_PUBLIC_URL`
   is set too: message links point at it.
3. `sudo systemctl restart wisen`. `curl https://<host>/health` should show
   `"telegram": true`, and `journalctl -u wisen` should show no
   `[telegram]` errors.
4. Link your chat: in the dashboard open **Devices → Add device** and copy
   the one-time code (the same code `wisen cloud pair` takes; it is valid
   for 10 minutes and works once, for a laptop or for Telegram). In a private
   chat with your bot, send `/link <code>`. The bot replies "Linked to
   <user>".

Then:

- Tap **Approve** or **Reject** on a proposal. It is the same decision as in
  the web app: whichever you use first wins, and the other one says it was
  already decided.
- Send any question ("how was my week?") and your laptop's chat agent
  answers from your reflections. `/search <words>` also searches the raw
  history on the laptop. Only the written answer comes back. If the laptop
  is asleep the bot says the question is queued, and answers when the
  laptop's worker wakes.
- `/unlink` stops the bot messaging that chat. Messages from any chat that
  is not linked are ignored, and group chats cannot link.

To change the token (say it leaked), send `/revoke` to @BotFather, put the
new token in the env file and restart; the linked chat stays linked.

## Backups and restore

`wisen-backup.timer` runs `wisen cloud backup` daily (and at the next boot if
the box was off). It writes a verified, consistent snapshot to
`/var/lib/wisen/backups/wisen-<UTC time>.db` and keeps the newest 14. The
database holds packets and results only, never raw transcripts, so copying
the snapshots off the box is low-risk: uncomment the `ExecStartPost` line in
`wisen-backup.service` and point it at your remote. Claude Code deletes local
transcripts after 30 days by default, so this copy becomes the long-term
record.

Restore (test this once):

```bash
sudo systemctl stop wisen
sudo -u wisen /opt/wisen/bin/wisen cloud restore /var/lib/wisen/backups/wisen-<time>.db \
    --db /var/lib/wisen/wisen.db
sudo systemctl start wisen
```

`restore` checks the snapshot's integrity and refuses while the server is
running (it holds a lock on `wisen.db.lock` until it exits). Logins, paired
devices and their tokens come back with the data.

## Checks on the box (step 4)

After the owner says so, verify on the real box: pair the laptop; a day's
reflection arrives and the phone gets a push; a revoked token gets 401; a
server restart mid-lease recovers the job; a restored backup serves the same
data; the server's environment has no model key; no fixture marker string
appears in the server database. `python3 -m tests.test_cloud` runs the same
checks locally against an in-process server.

## Checks with a real bot (step 6)

`python3 -m tests.test_telegram` runs these against a fake Telegram API. With
the real bot set up as above: link the chat; approve a proposal in Telegram,
then try it in the web app (it says already decided, and the proposal has one
decision); a message from another Telegram account gets no reply; with the
laptop asleep, `/search <words>` replies that it is queued and the answer
arrives once the laptop wakes; no proposal message shows a diff or a quote.
