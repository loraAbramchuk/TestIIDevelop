# Procrastinate schema snapshot

`procrastinate-3.11.0.sql` is the unmodified `procrastinate/sql/schema.sql`
distributed with the pinned Procrastinate 3.11.0 package.

SHA-256: `d1f91ba563f92376c631a7d88cb54a5fdc849d5ff104fcc6d157a8a1227f779f`.

The MIT license is included in `procrastinate-LICENSE.md`. Migration 0004 checks
this digest, installs the snapshot under `video_queue` and pins the functions'
search path to that namespace. Keep this historical snapshot immutable; future
library/schema upgrades need a new migration.
