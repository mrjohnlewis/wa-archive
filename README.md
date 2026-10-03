# wa-archive

A personal tool that archives WhatsApp history from encrypted iPhone backups
(made with Finder) into a master archive in iCloud Drive. The archive only grows.
That lets you clear WhatsApp media on the phone without losing anything.

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

## Routine (every few months)

1. **Finder backup.** Connect the iPhone, then Finder → General → *Encrypt local backup* → *Back Up Now*.
2. **Optional preview:** `uv run wa-archive ingest --dry-run` shows new files and bytes, and how many batches, without writing anything.
3. **Ingest:** `uv run wa-archive ingest` merges the newest backup into the archive.
   - It's read-only against the backup and append-only against the archive.
   - It keeps the Mac awake (`caffeinate`) and can be re-run at any time. An interrupted run resumes from its last published batch.
   - If new media doesn't fit on disk, it works in batches. Between batches it waits for iCloud to upload, hash-checks the files, then evicts the local copies.
4. **Report:** `uv run wa-archive report --wait-upload 6`. This waits up to 6 h for iCloud uploads, re-hashes every local archived file, and prints the **safe to clear** list. A copy is saved in `reports/` in the archive.
   - A chat is listed only when every media file in the backup for that chat is archived, hash-verified and uploaded to iCloud.
   - The archive database must also be uploaded.
   - The backup time is shown at the top. Anything received after it isn't archived, but Manage Storage clears it anyway.
   - If the backup is more than 6 h old, you get a red warning.
   - **After a long first ingest, take a fresh Finder backup and run `ingest` again.** That's quick, since existing files are skipped, and it keeps the age check green.
5. **On the phone:** WhatsApp → Settings → Storage and Data → Manage Storage → clear media only for the chats on the safe list.
6. Optionally delete old Finder backups (Finder → Manage Backups). wa-archive never deletes anything.

`uv run wa-archive evict` frees Mac disk immediately. It evicts archived media that's verified and uploaded; the files stay in iCloud.

## Browsing the archive

```sh
uv run wa-archive serve
```

This opens the archive in your browser at a private `http://127.0.0.1:<port>/?t=<secret>` link. The viewer:
- only answers on this Mac;
- works only through that link, which is new on every launch;
- never changes the archive.

Features:
- chats sorted by latest activity, with WhatsApp-style bubbles;
- photos, videos, GIFs, stickers, voice notes and documents shown inline;
- replies, reactions, the "edited" label with version history, and "deleted for everyone" messages kept and flagged;
- jump to a date, search across all chats or within one, and a call log;
- status updates and channels hidden by default (tick the box to show them).

Media that iCloud has evicted downloads on demand, so you'll see a short pause.
The viewer updates itself when an ingest runs, including mid-ingest after each batch. Within about 10 seconds the chat list refreshes and a notice shows how many messages arrived. New messages are added to the open chat if you're at the bottom; otherwise a **New messages ↓** button appears. Press Ctrl-C to stop.

Voice notes are Opus audio. Chrome plays them; if your browser can't, the
viewer offers a download link instead.

## Settings

```sh
uv run wa-archive config                                  # show the archive and backup folders, and where each value comes from
uv run wa-archive config set archive-dir "~/Library/Mobile Documents/com~apple~CloudDocs/Family/WhatsApp Archive"
uv run wa-archive config unset archive-dir                # back to the default
```

Settings are saved in `~/Library/Application Support/wa-archive/config.toml`.
`backup-root` can be set the same way. Environment variables (`WA_ARCHIVE_DIR`,
`WA_ARCHIVE_BACKUP_ROOT`) override saved settings.

**Changing `archive-dir` never moves your archive.** To relocate it:
1. Move the whole `WhatsApp Archive` folder in Finder and let iCloud finish syncing.
2. Then run `config set`.

`config set` refuses to point at an empty folder while an archive exists at the
current location. If the configured folder is missing an archive this Mac has
already published, every command stops with an explanation rather than
quietly starting a new archive.

## Where things live

| What | Where | Synced |
|---|---|---|
| Archive DB (published snapshot) | `iCloud Drive/WhatsApp Archive/archive.sqlite` + `archive.json` | yes |
| Media (content-addressed, immutable) | `…/WhatsApp Archive/media/ab/cd/<sha256>.<ext>` | yes |
| Previous DB versions (never deleted) | `…/WhatsApp Archive/snapshots/` | yes |
| Reports | `…/WhatsApp Archive/reports/` | yes |
| Working DB, temp files | `~/Library/Application Support/wa-archive/` | no |

The working DB is only ever written locally. Each publish writes a complete
copy and renames it into iCloud Drive in one step, so iCloud never syncs a
half-written database.

**Eviction trade-off.** Once media is verified and uploaded, iCloud may remove
the local copy (macOS does this under storage pressure, and `wa-archive evict`
does it on request). That saves Mac disk, but:
- viewing that media needs a network connection, since it downloads on demand;
- your only complete copy is then in iCloud.

If you want a second copy, keep the folder downloaded on another disk or Mac.
Time Machine only backs up files that are present locally.

## Restoring on a new Mac

1. Sign in to the same Apple Account and let iCloud Drive sync `WhatsApp Archive`.
2. Install: `brew install uv`, clone this repo, `uv sync`.
3. `uv run wa-archive restore` downloads `archive.sqlite` if needed, checks it against the SHA-256 in `archive.json` and runs an integrity check. It then rebuilds the local working copy. (`ingest` and `report` do this automatically too.)
4. Carry on with the routine. Media downloads from iCloud as it's needed.

## Privacy

- The archive is stored **unencrypted** inside iCloud Drive. It is end-to-end
  encrypted only if Advanced Data Protection is on for your Apple Account. On
  the Mac it's protected by FileVault.
- Logs and errors never contain message contents. Use `--debug` only with
  synthetic test data.
- Tests use synthetic fixtures only: `uv run pytest`.
