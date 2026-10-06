# Security

wa-archive handles private conversations, so security reports are taken seriously.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/mrjohnlewis/wa-archive/security/advisories/new).

Include the version or commit, what an attacker could do, and steps to reproduce.
Use synthetic data only; never include real messages or backups.

This is a personal project maintained on a best-effort basis. You'll get an
acknowledgement when the report is seen, and a fix as soon as reasonably possible.

## Design notes relevant to security

- **Reading backups:** the tool reads iPhone backups only; it never writes to them. Decrypted WhatsApp
  databases go to a private (mode 700) temp folder and are deleted after each run.
- **Backup password:** taken from an interactive prompt or the macOS Keychain. It is never placed in
  files, command-line arguments, environment variables or logs.
- **The viewer:**
  - listens only on `127.0.0.1`;
  - requires a per-launch secret and a localhost `Host` header, which blocks DNS rebinding;
  - sends a strict Content-Security-Policy, never inserts message content as HTML, and is read-only.
- **Network:** the tool makes no network requests of its own.
