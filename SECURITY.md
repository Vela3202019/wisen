# Security policy

## Supported versions

wisen is pre-1.0 and moves fast. Fixes land on `main` and in the next release;
only the latest release gets security fixes.

| Version         | Supported |
| --------------- | --------- |
| latest release  | yes       |
| older releases  | no        |

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting: open the
[Security tab](https://github.com/Vela3202019/wisen/security), then
**Report a vulnerability**. Please don't open a public issue, discussion or PR
for a security problem.

Include what you found, how to reproduce it, and what an attacker could do with
it. This is a personal project, so expect a first reply within about a week. I
will keep you posted while a fix is in progress and credit you in the release
notes unless you'd rather not be named.

## What's in scope

wisen reads your coding-agent transcripts, git history and agent configs on your
own machine. Anything that breaks the promises in
[What leaves your machine](docs/privacy.md) is in scope, for example:

- transcript content, tool output or secrets leaving the machine, or reaching
  your server past the egress gate;
- raw session content written to the SQLite store or logs;
- the Gardener writing outside the files it is allowed to edit;
- auth or pairing flaws in the optional server (`wisen cloud serve`), the
  approval inbox or the Telegram bot.

Out of scope: what your chosen model provider does with the excerpts wisen is
documented to send it, and vulnerabilities in third-party dependencies that
don't affect wisen (report those upstream).
