# wa-archive

**Free up WhatsApp space on your iPhone without losing a single photo, video or message.**

`wa-archive` reads the encrypted iPhone backup that Finder makes on your Mac and
copies your WhatsApp history into an archive in your iCloud Drive. That
history covers chats, photos, videos, voice notes, documents, reactions and
calls. The archive only ever grows. Run it again a few months later and it
adds what's new, while keeping everything it already had, including media you
have since cleared from the phone.

Before you delete anything, `wa-archive report` tells you, chat by chat, which
ones are **safe to clear**. A chat is listed only when every media file in it is
archived, hash-verified and uploaded to iCloud. A local viewer then lets you
browse and search the whole archive in your browser.

> **Status:** early release (0.x). Built for one family's archive and shared
> as-is in case it helps others. Support is best effort; see [Support](#support).
> Not affiliated with, endorsed by or connected to WhatsApp or Meta.

---

## Contents

- [Is this for you?](#is-this-for-you)
- [How it keeps your data safe](#how-it-keeps-your-data-safe)
- [Privacy](#privacy)
- [Install](#install)
- [First run](#first-run)
- [Clearing media on the phone](#clearing-media-on-the-phone)
- [The routine, every few months](#the-routine-every-few-months)
- [Browsing the archive](#browsing-the-archive)
- [Commands](#commands)
- [Settings](#settings)
- [Where things live](#where-things-live)
- [Restoring on a new Mac](#restoring-on-a-new-mac)
- [Compatibility](#compatibility)
- [Troubleshooting](#troubleshooting)
- [Reporting a problem](#reporting-a-problem)
- [Support](#support) · [Development](#development) · [Acknowledgements](#acknowledgements) · [License](#license)

---

## Is this for you?

You'll need:

- **A Mac** (tested on macOS 26) and **an iPhone** that you back up to that Mac with Finder.
- **Encrypted backups** turned on in Finder. The tool reads encrypted backups only.
- **iCloud Drive** turned on, with enough free iCloud storage for your WhatsApp media.
  The archive lives there, and "safe to clear" depends on it being uploaded.
- **Some comfort with the Terminal.** Everything runs from the command line,
  apart from the browser viewer.

It's **not** for:
- Android phones;
- WhatsApp Business;
- Windows;
- keeping the archive on Dropbox, Google Drive, a NAS or an external disk (see [Compatibility](#compatibility)).

## How it keeps your data safe

- **Your backup and your phone are never touched.** The tool only reads the
  Finder backup. Clearing media on the phone is always something you do yourself.
- **The archive never forgets.** Messages and media are never removed or
  overwritten. The database enforces this with triggers, not just by
  convention. Edits are kept as extra versions. "Deleted for everyone"
  messages you'd already archived are kept and flagged.
- **Media is stored by content hash** (SHA-256). Identical files, such as a
  photo forwarded to five groups, are stored once, and every file can be
  re-verified.
- **iCloud never syncs a half-written database.** Work happens on a local copy.
  Each finished step is published to iCloud Drive as a complete file, in one
  atomic rename.
- **"Safe to clear" is strict.** Every media file the backup holds for a chat
  must be archived, hash-verified and fully uploaded to iCloud, and so must
  the archive database itself. Otherwise the chat isn't listed. The report also
  warns loudly if your backup is more than a few hours old, because anything
  newer isn't archived yet.
- **Interruptions are safe.** If a run stops partway (crash, closed lid, full
  disk), just run the command again. It continues from the last saved point
  and doesn't duplicate anything.

## Privacy

- **Nothing leaves your Mac except through iCloud Drive.** No telemetry,
  analytics or network calls of its own. The viewer listens only on
  `127.0.0.1`, behind a secret link that changes every time you start it.
- **Message contents never appear in logs, errors or reports.** Reports show
  chat names and counts only.
- **Your backup password** is typed at a prompt each time, or kept in your
  macOS Keychain if you choose. It's never stored in a file, passed on the
  command line, or logged.
- **The archive itself is not encrypted by this tool.** In iCloud it's
  end-to-end encrypted only if you've turned on **Advanced Data Protection**
  for your Apple Account. On your Mac it's protected by FileVault. Consider
  turning on both.

## Install

1. Install [uv](https://docs.astral.sh/uv/), a Python package and project manager, if you don't have it:
   ```sh
   brew install uv        # or: curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Get the code:
   ```sh
   git clone https://github.com/mrjohnlewis/wa-archive.git
   cd wa-archive
   uv sync
   ```
3. Give your Terminal app **Full Disk Access**: System Settings → Privacy & Security
   → Full Disk Access → turn on Terminal (or iTerm, etc.). Then restart it.
   macOS needs this before any app can read iPhone backups.

Run every command from the `wa-archive` folder, prefixed with `uv run`.

## First run

1. **Make an encrypted backup.** Connect your iPhone, open it in Finder, and
   under *Backups* choose "Back up all of the data on your iPhone to this Mac".
   Tick **Encrypt local backup** (keep the password safe), then **Back Up Now**.
2. **Check what the tool can see.** This needs no password:
   ```sh
   uv run wa-archive backups
   ```
   It lists each backup with device name, iOS version, date and size. The
   newest one is used by default.
3. **Optional:** save the backup password in your Keychain, so you aren't
   asked for it every time:
   ```sh
   uv run wa-archive password --save
   ```
4. **Preview** what will be archived. This writes nothing:
   ```sh
   uv run wa-archive ingest --dry-run
   ```
   It shows the number of messages and files, the total size, and whether it
   all fits on your disk at once. If it doesn't, ingest works in batches
   automatically.
5. **Ingest:**
   ```sh
   uv run wa-archive ingest
   ```
   For a typical phone this takes minutes, not hours. For example, 93,000
   messages and 10 GB of media took about 3 minutes. Your Mac is kept awake
   while it runs.
6. **Report:**
   ```sh
   uv run wa-archive report --wait-upload 6
   ```
   This waits up to 6 hours for iCloud to finish uploading. It then re-checks
   every local file and prints a table of chats and the **Safe to clear media**
   list. A copy is saved in the archive's `reports/` folder.
7. **Browse it:**
   ```sh
   uv run wa-archive serve
   ```

## Clearing media on the phone

Before you clear anything, make sure the report's backup time is recent (the
header is green, not red) and that the chats you want are on the safe list.
If time has passed, take a fresh Finder backup and run `ingest` again. Only new
items are copied, so it's quick.

Then, in WhatsApp: **Settings → Storage and Data → Manage Storage**.

1. Tap a chat that's on your safe list. The biggest chats are at the top.
2. Tap **Select** → **Select All** → the trash icon, and confirm.
3. Repeat for each chat you want to clear.

This removes the media only; your messages stay on the phone.

> ⚠️ **Don't** use **Clear Chat** or **Delete Chat** from a chat's info screen.
> Those delete the messages too.

Afterwards, a good **proof check** is to take another Finder backup and run
`ingest` then `report`. You should see about 0 new messages, 0 new media, and
no problems. Your cleared chats' media should still be counted as archived,
and still be visible in the viewer.

Things to know:
- **Save to Photos:** if "Save to Photos" is on in WhatsApp (Settings → Chats),
  many photos also live in your Photos library. Clearing WhatsApp media doesn't
  remove those copies.
- **Media auto-download:** keep it on if you can. Media your phone never
  downloaded can't be archived, because it isn't in the backup.

## The routine, every few months

1. Finder backup (encrypted).
2. `uv run wa-archive ingest`
3. `uv run wa-archive report --wait-upload 6`
4. Clear media on the phone, only for chats on the safe list.
5. Optionally delete old Finder backups (Finder → Manage Backups).
   `wa-archive` never deletes anything itself.

If your Mac is short of space after the upload, `uv run wa-archive evict`
removes local copies of archived media that's verified and safely in iCloud.

## Browsing the archive

```sh
uv run wa-archive serve
```

This opens your browser at a private link like `http://127.0.0.1:<port>/?t=<secret>`.

- **Chat list:** sorted by latest activity. Status updates and channels are hidden unless you tick the box.
- **Chats:** WhatsApp-style bubbles, with photos, videos, GIFs, stickers, voice notes and documents shown inline.
- **Message details:** replies (click one to jump to the original), reactions, an "edited" label with earlier versions, and "deleted for everyone" messages kept and flagged.
- **Finding things:** jump to a date, search all chats or just the current one, and a call log.
- **Large chats:** long histories stay smooth. Scrolling loads more as you go, and the page never holds more than a few hundred messages at once.
- **Live updates:** while an ingest runs, the viewer picks up new messages by itself.

The viewer is read-only. Press Ctrl-C in the Terminal to stop it.

Notes:
- **Evicted media:** if iCloud has removed a file's local copy, it's downloaded
  when you open it, so expect a short pause.
- **Voice notes** are Opus audio. Chrome plays them. In a browser that can't,
  the viewer offers a download link instead.
- **Sender names:** group members who aren't in your contacts, and whom WhatsApp
  identifies only by an anonymous ID, may appear as numbers.

## Commands

| Command | What it does |
|---|---|
| `backups` | List iPhone backups on this Mac. No password needed. |
| `ingest [--dry-run]` | Merge the newest backup into the archive. |
| `report [--wait-upload HOURS] [--all]` | Verify the archive and list the chats that are safe to clear. |
| `serve` | Browse the archive in your web browser. |
| `evict` | Free Mac disk by removing local copies of media that's verified and uploaded. |
| `restore` | Rebuild local state from the archive folder, e.g. on a new Mac. |
| `config [set\|unset ...]` | Show or change where the archive and backups live. |
| `password [--save\|--forget]` | Manage the Keychain-saved backup password. |
| `check` | Compatibility check: prints counts and structure only. Useful for bug reports. |

Run `uv run wa-archive <command> --help` for all options.

## Settings

```sh
uv run wa-archive config                      # show settings and where each value comes from
uv run wa-archive config set archive-dir "~/Library/Mobile Documents/com~apple~CloudDocs/Family/WhatsApp Archive"
uv run wa-archive config unset archive-dir    # back to the default
```

- The default archive location is `iCloud Drive/WhatsApp Archive`.
- `backup-root` can be changed the same way. The default is Finder's standard backup folder.
- Settings are saved in `~/Library/Application Support/wa-archive/config.toml`.
- The environment variables `WA_ARCHIVE_DIR` and `WA_ARCHIVE_BACKUP_ROOT` override saved settings.

**Changing `archive-dir` doesn't move your archive.** To relocate it, move the
whole `WhatsApp Archive` folder in Finder, let iCloud finish syncing, then run
`config set`. The command refuses to point at an empty folder while an archive
still exists at the old location. If the configured folder is missing an archive
this Mac has already published, every command stops and explains why, instead
of quietly starting a new archive.

## Where things live

| What | Where | In iCloud |
|---|---|---|
| Archive database (published copy) | `WhatsApp Archive/archive.sqlite` + `archive.json` | yes |
| Media (immutable, named by SHA-256) | `WhatsApp Archive/media/ab/cd/<sha256>.<ext>` | yes |
| Previous database versions (kept) | `WhatsApp Archive/snapshots/` | yes |
| Reports | `WhatsApp Archive/reports/` | yes |
| Working database, settings, temp files | `~/Library/Application Support/wa-archive/` | no |

**Eviction trade-off.** Once media is verified and uploaded, macOS may remove
the local copy to save space. This happens when "Optimize Mac Storage" is on, or
when you run `evict`. That saves disk, but viewing the media then needs a
network connection, and your only full copy is in iCloud. If you want a second
copy, keep the folder downloaded on another disk or Mac. Time Machine only backs
up files that are present locally.

## Restoring on a new Mac

1. Sign in to the same Apple Account and let iCloud Drive sync `WhatsApp Archive`.
2. Follow [Install](#install).
3. Run `uv run wa-archive restore`. It downloads the archive database if needed and
   checks it against the hash in `archive.json`. It also runs an integrity check,
   then rebuilds the local working copy.
4. Carry on with the routine. Media downloads from iCloud as it's needed.

## Compatibility

Tested with:

| | Version |
|---|---|
| macOS | 26.6 |
| iOS (backup) | 27.0 |
| WhatsApp (iPhone) | 26.38.74 |
| Python | 3.13 (installed by `uv`) |

WhatsApp changes its internal database from time to time. The tool reads only
the parts it needs and keeps a raw copy of every message row, so details it
doesn't understand yet aren't lost. Message types it doesn't recognise are still
archived, and the viewer shows them as "Unsupported". Other iOS and WhatsApp
versions may still behave differently. Run `wa-archive check` first: if it
reports missing columns, please [open an issue](#reporting-a-problem) before
relying on the tool.

Not supported:
- Android;
- WhatsApp Business;
- Windows;
- unencrypted backups;
- archives stored outside iCloud Drive.

The report will never mark chats safe when the archive is outside iCloud Drive,
because it can't confirm there's an off-Mac copy.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `backups` lists nothing, or "Operation not permitted" | Give your Terminal app Full Disk Access, then restart it ([Install](#install), step 3). |
| "This backup is not encrypted" | Tick *Encrypt local backup* in Finder, then back up again. |
| "Wrong backup password" | It's the Finder backup password, not your Apple Account password. If you saved it, run `wa-archive password --forget` and try again. |
| The report says files are "pending upload" | iCloud is still uploading. Run `report --wait-upload 6`, or try again later. Check that iCloud Drive isn't paused (Low Power Mode, or a hotspot in Low Data Mode). |
| The safe list is empty and the report shows "not in iCloud Drive" | The archive folder is outside iCloud Drive; see [Settings](#settings). |
| "Not enough free disk space" | Free some space, or run `wa-archive evict` once earlier media has uploaded. |
| "No archive found at …, but this Mac has already published one" | The archive folder moved, or iCloud is still downloading it. See [Settings](#settings). |
| "written by a newer version of wa-archive" | Update the tool: `git pull && uv sync`. |
| A voice note won't play in the viewer | Use Chrome, or use the download link. |

## Reporting a problem

Please [open an issue](https://github.com/mrjohnlewis/wa-archive/issues) with:

- the output of `uv run wa-archive check`, which shows counts and structure only;
- your macOS, iOS and WhatsApp versions;
- the exact error message.

> 🔒 **Never post message text, chat names, phone numbers or screenshots of your
> chats.** They belong to you *and* to the people you talk to. The `check`
> output contains no message contents, so it's safe to share.

Security issues: please report privately (see [SECURITY.md](SECURITY.md)).

## Support

This is a personal project shared in the hope it's useful. Issues and pull
requests are welcome and will be looked at on a best-effort basis, with no
guarantees of fixes or timelines. Please read the license's "no warranty"
section. You're responsible for checking the report before you clear anything
on your phone.

## Development

```sh
uv sync --all-groups
uv run --group crosscheck pytest
```

All tests use **synthetic** data (`tests/fixture.py`). Never add real backups,
real chats or real names to the repository, tests or issues. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

- [iphone_backup_decrypt](https://github.com/jsharkey13/iphone_backup_decrypt)
  by James Sharkey decrypts the iPhone backups.
- [WhatsApp-Chat-Exporter](https://github.com/KnugiHK/WhatsApp-Chat-Exporter)
  by KnugiHK was the reference for WhatsApp's iOS database layout. It's also
  used by `check --crosscheck` to cross-check message counts.
- Built with the help of [Claude Code](https://claude.com/claude-code).

## License

[MIT](LICENSE) © 2026 John Lewis

WhatsApp is a trademark of Meta Platforms, Inc. This project is independent and
is not affiliated with or endorsed by Meta.
