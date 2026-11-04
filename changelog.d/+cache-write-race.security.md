**Concurrent commands no longer revert each other's exploration cache writes.**
A command that writes the cache (`explore profile`, `explore relationships`,
`explore map`, and `explore query` or `explore cluster` when they profile an
object on demand) merged its profiles into the copy it loaded at its start and
saved that, so a profile another command saved in the meantime was overwritten.
That included a PII flag a refresh had just raised, which the query firewall then
read at its old confidence until the next refresh. Only commands running at the
same time against one `.dex/` were affected; one command at a time never was.

- Every cache write now re-reads the stored cache under a new store-level cache
  lock, folds its own profiles into it, keeps any stored profile newer than the
  copy it carried, and saves before releasing the lock. No warehouse query runs
  under the lock.
- The filesystem and SQLite backends hold the lock on `.dex/cache.lock`, apart
  from the spend lock. The in-memory backend holds it across threads.
- A storage backend you wrote can offer the same lock through the new optional
  `CacheLock` protocol (`cache_lock(timeout=...)`). Without it, dex still re-reads
  the cache before merging, which narrows the window without closing it.

Reported by [@catincloudlabs](https://github.com/catincloudlabs).
