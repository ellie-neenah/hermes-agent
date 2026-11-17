# Contributor email → GitHub login mappings

This directory replaces appending entries to `AUTHOR_MAP` in
`scripts/releases/authors_legacy.py`. New mappings are stored as individual,
hash-named JSON records so filenames do not expose contributor email addresses.

## Adding a mapping

Add a mapping with:

```bash
python3 scripts/add_contributor.py <email> <github-login>
```

- Records are stored under `emails/by-hash/`, with a SHA-256 filename derived
  from the exact commit-author email.
- Each JSON record stores the email, GitHub login, and optional comment.
- Existing `emails/<email>` files remain readable; new mappings are never
  written using email as a filename.

## Rules

- Do NOT add new entries to `AUTHOR_MAP` in `scripts/releases/authors_legacy.py`. That dict
  is frozen legacy data; release tooling merges it with these records
  (contributor records win on duplicates).
- GitHub noreply emails (`<id>+<login>@users.noreply.github.com` and
  `<login>@users.noreply.github.com`) auto-resolve — no file needed.
- The `Contributor Attribution Check` CI job fails a PR whose commits carry
  an unmapped email; the failure message prints the exact command to run.
