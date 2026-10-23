"""
wisen's server, stage A: a mailbox. It holds what paired laptops send (result
and proposal packets), the human's decisions, the schedule and the device
jobs, and it pushes notifications. It makes no model calls and holds no model
key; all model work stays on the laptop. See docs/plan/cloud-architecture.md
§3.1 and §8.

  mailbox.py  the server's SQLite file and every rule about it
  server.py   the HTTP app: browser login, approval inbox, device API
  push.py     Web Push (VAPID) sender
  backup.py   consistent snapshots and restore
  client.py   laptop side: pairing and the device API client
  worker.py   laptop side: `wisen worker`, the outbound-only poller
"""
