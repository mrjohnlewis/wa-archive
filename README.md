# wa-archive

A personal tool that archives WhatsApp history from encrypted iPhone backups
(made with Finder) into a master archive in iCloud Drive. The archive only grows.
That lets you clear WhatsApp media on the phone without losing anything.

> Status: **phase 1 (extraction spike)**. `ingest`, `report` and `serve` arrive in
> phases 2–3. The full routine below describes the target workflow.

## Setup

```sh
brew install uv
cd ~/Dev/WA-archive
uv sync                       # add --group crosscheck to enable `spike --crosscheck`
```

Your terminal app needs **Full Disk Access** (System Settings → Privacy & Security)
to read `~/Library/Application Support/MobileSync/Backup`.

## Backup password

The backup password is never passed on the command line or kept in plaintext.

```sh
uv run wa-archive password --save     # optional: store it in your login Keychain
uv run wa-archive password            # is one saved?
uv run wa-archive password --forget
```

Without a saved password, each run prompts for it.

## Phase 1: spike

```sh
uv run wa-archive backups                          # device, iOS, date, size of each backup
uv run --group crosscheck wa-archive spike --crosscheck
```

`spike` decrypts only WhatsApp's databases into a private temp folder, which it
deletes afterwards. It prints **structure and counts only**: no message text,
names, numbers or file names. A copy of that output is saved under
`~/Library/Application Support/wa-archive/spike/`.

## Routine (target, every few months)

1. **Finder backup**: connect the iPhone, Finder → General → *Encrypt local backup* → *Back Up Now*.
2. `wa-archive ingest`: merges the newest backup into the archive. It is read-only
   against the backup. When new media won't fit on disk it runs in batches, and
   between batches it waits for iCloud to upload, then evicts the local copies.
3. `wa-archive report`: the per-chat "safe to clear media" list. A chat qualifies
   only when every one of its media files is archived, hash-verified and
   uploaded to iCloud.
   After a long first ingest, **take a fresh Finder backup and ingest again**.
   That's quick, and it keeps the backup-age warning green.
4. On the phone: WhatsApp → Settings → Storage and Data → Manage Storage → clear
   media for each chat on the safe list.
5. Optionally delete old Finder backups (Finder → Manage Backups).

## Privacy

- The archive is stored **unencrypted** inside iCloud Drive. It is end-to-end
  encrypted only if Advanced Data Protection is on for your Apple Account. On
  the Mac it's protected by FileVault.
- Logs and errors never contain message contents. Use `--debug` only with
  synthetic test data.
- Tests use synthetic fixtures only: `uv run pytest`.
