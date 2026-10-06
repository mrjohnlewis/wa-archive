# Changelog

This project follows [Semantic Versioning](https://semver.org/). Before 1.0, minor versions may
change behaviour; the archive format is versioned and older tools refuse newer archives.

## 0.1.0: first public release

- **`ingest`:** an incremental, append-only archive built from encrypted Finder backups, stored in
  iCloud Drive.
  - stable message identity (survives the phone-number-to-LID address change);
  - edits and "deleted for everyone" kept as versions;
  - reactions, quoted replies, calls and contact names;
  - media stored by content hash (SHA-256), including thumbnails;
  - batched and resumable when disk space is short.
- **`report`:** a per-chat "safe to clear" list, which requires hash-verified media fully uploaded to
  iCloud, with a backup-age warning.
- **`serve`:** a local, read-only browser viewer with search, jump to date, inline media and live
  updates.
- **Other commands:** `evict`, `restore`, `config`, `password`, `backups`, and `check`
  (a compatibility check for bug reports).
