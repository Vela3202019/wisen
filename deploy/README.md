# Self-hosting the wisen server

In stage A (the default) the server is a mailbox: it stores what your laptop
sends (reflections as result packets, Gardener proposals), holds your
decisions, keeps the daily schedule, and sends Web Push notifications. It
makes no model calls and holds no model API key. In stage B it also runs the
Synthesizer and the Gardener over the packets your laptops send, so the
morning reflection is made while they sleep; see
[Stage B](#stage-b-the-server-reflects). Either way raw transcripts never
reach it, and the laptop only ever connects out to it, with a per-device
token you can revoke.

Everything here is a sample for one Ubuntu box with Caddy already serving
HTTPS. Nothing in the app is specific to that box: it is configured by
environment variables only.

| File | Goes to |
|---|---|
| `wisen.service` | `/etc/systemd/system/wisen.service` |
| `wisen-user.service` | instead, `~/.config/systemd/user/wisen.service`: a user-level unit run as yourself |
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

# Secrets: VAPID keys for Web Push (no model key)
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
**Upgrade the server before (or together with) the laptops:** a newer
laptop can send what an older server's packet check refuses (stage B's
observation and config snapshot packets, a `skipped` job status).

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

## Stage B: the server reflects

Stage B moves the Synthesizer and the Gardener here
(docs/plan/cloud-architecture.md §3.2). The laptops keep the raw history and
run the Observer; they send its notes (observation packets) and the current
text of the files the Gardener may edit (config snapshot packets,
secret-redacted). At `--daily-at` the server asks the laptops for fresh notes,
waits up to 30 minutes, then synthesizes the day from every laptop's notes
as one reflection, runs the Gardener, and sends one push. Approved changes are
still applied by the laptop whose file they change.

Synthesis runs on `claude -p`, on the owner's Claude subscription (the
owner's own box, for the owner's own use; a server that serves other people
must use an API key). The Gardener uses tools, which `claude -p` does not
serve, so for now it runs on `WISEN_MODEL` and its API key. Reflection chat
runs on DeepSeek, through pydantic-ai's DeepSeek provider. `WISEN_SYNTH_MODEL=api`
moves synthesis to the API model too: that is the fallback, by config.

What each job needs:

| Job        | Runs on                         | Needs                                         |
|------------|---------------------------------|-----------------------------------------------|
| Synthesis  | `claude -p` (`WISEN_SYNTH_MODEL`) | the CLI logged in for the service's user (step 2) |
| Chat       | DeepSeek (`WISEN_CHAT_MODEL`)    | `DEEPSEEK_API_KEY`                            |
| Gardener   | `WISEN_MODEL`                   | that provider's API key                       |

1. Upgrade the server first (see above), then add to `/etc/wisen/wisen.env`:

   ```bash
   WISEN_CLOUD_STAGE=b
   WISEN_MODEL=anthropic:claude-sonnet-5   # the Gardener; any pydantic-ai model string
   ANTHROPIC_API_KEY=…                     # the key for that provider
   WISEN_CHAT_MODEL=deepseek-v4-flash      # chat: the DeepSeek model name; no default
   DEEPSEEK_API_KEY=…                      # chat's key; no default
   # WISEN_SYNTH_MODEL=claude-cli:sonnet   # the default in stage B; "api" falls back to WISEN_MODEL
   ```

   The keys live only in the env file; wisen never prints or logs them, nor
   puts them in a packet (the start-up line names which key variables are
   set, never their values).
2. Log the CLI in **for the user the service runs as**, once, and make sure
   the service can read that login:
   - `deploy/wisen.service` runs as `wisen` with `ProtectHome=true`, so it
     cannot see a login under `/home`. Install Claude Code for `wisen`, log
     in as it (`sudo -u wisen -H claude`, then `/login`); its login and state
     land in `/var/lib/wisen/.claude`, inside `ReadWritePaths`. Put the CLI on
     the unit's `PATH`: uncomment the `Environment=PATH=…` line.
   - `deploy/wisen-user.service` (a user-level unit, run as your own user, as
     on the owner's box) reads your own `~/.claude` login, which `claude`
     itself keeps there; it sets no `ProtectHome`, and its `PATH` includes
     `~/.local/bin`.
   - An expired login fails the day's synthesis job with the CLI's
     "Failed to authenticate" message (the job retries, then shows as
     failed); log in again, or set `WISEN_SYNTH_MODEL=api`.
3. Restart the service. `curl https://<host>/health` shows `"stage": "b"`,
   `"synthesis": "claude-cli"` and `"model_key": true`.
4. On each laptop: `[cloud] stage = "b"` in `~/.wisen/config.toml` (or
   `WISEN_CLOUD_STAGE=b` in the worker's environment), then restart
   `wisen worker`. A laptop left on stage A refuses the server's requests for
   notes (its job fails with `StageBOff`) and keeps sending reflections the
   stage-A way.

**Memory.** Measured on the owner's 3.7 GiB box on 2026-10-28 (Claude Code
2.1.272, the flags wisen passes): one `claude -p` process peaks at about
**236 MiB** before its model call. The box's login had expired on 2026-09-15,
so a full call was not measured yet; re-measure once the CLI is logged in
again (an interactive session there sits at about 329 MB). The CLI runs in
the service's cgroup next to the server (~130 MB), so both units set
`MemoryHigh=600M` and `MemoryMax=800M`. The box had about 1.6 GiB available.

**Chat.** `POST /api/chat` (logged in, stage B only) answers questions from
the server's reflections and streams the answer as server-sent events. It is
stateless: the caller sends the conversation each turn, and nothing is
stored. It runs on DeepSeek, so the reflection text it answers from (already
redacted by the egress gate on the laptop) and the questions asked reach
DeepSeek's servers. Without `WISEN_CHAT_MODEL` or `DEEPSEEK_API_KEY`, chat
answers with an error; synthesis and the Gardener are not affected.

## Checks on the box (step 4)

After the owner says so, verify on the real box: pair the laptop; a day's
reflection arrives and the phone gets a push; a revoked token gets 401; a
server restart mid-lease recovers the job; a restored backup serves the same
data; the server's environment has no model key; no fixture marker string
appears in the server database. `python3 -m tests.test_cloud` runs the same
checks locally against an in-process server.
