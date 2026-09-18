# archive/

A durable, plain-text copy of every article the site has displayed.

Everything else the app knows lives in one SQLite file
(`backend/data/dailybrief.db`), and that file is a single point of
failure for the whole corpus: a publisher deleting a story, a paywall
going up, a prune running, or the file corrupting all end the same way.
This directory is the copy that survives those.

Written by `backend/agent/archivist.py`.

## Layout

```
archive/2026/09/2026-09-18.ndjson
```

One directory per year and month, one file per **publication** date, so
a day's news lands together and no file grows past what a human can
open.

## Format

NDJSON — one JSON object per line, appended, never rewritten. No
database and no framework needed to read it:

```bash
# every headline from one day
jq -r '.title' archive/2026/09/2026-09-18.ndjson

# only stories whose body we captured
jq -c 'select(.has_body)' archive/2026/09/2026-09-18.ndjson

# everything from one outlet, across the whole archive
cat archive/*/*/*.ndjson | jq -c 'select(.outlet == "Reuters")'

# how many articles are stored
cat archive/*/*/*.ndjson | wc -l
```

Each line carries the feed metadata (`title`, `outlet`, `category`,
`score`, `published_at`, the Korean translation if one exists) plus
`body` — the extracted article text as a list of paragraphs. `has_body`
says whether the text was captured or only the metadata.

Append-only is deliberate. An interrupted append costs at most one
malformed trailing line, which the reader skips; an interrupted rewrite
would cost the entire day.

## How it relates to the prune

```
display → record_displayed() → archivist worker → prune
```

The archivist stamps `articles.archived_to_disk_at` only **after** the
file write returns. The prune deletes exclusively stamped rows — so an
article cannot be removed from the database before a copy of it exists
here. If the archivist falls behind, the prune removes less; it never
removes something unsaved.

## Not in git

The contents are gitignored. This is operational data that grows without
bound, it differs per deployment, and it would bloat the repository.
Back it up the way you'd back up a database — the whole point is that it
is just files, so `rsync`, `tar`, or `scp` all work:

```bash
rsync -avz ubuntu@<box>:~/dailybrief/archive/ ./archive-backup/
```
