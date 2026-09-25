# pg_spectrograph (pgspec)

A read-only, privacy-preserving performance signature extractor for PostgreSQL.

Design spec: [`frozen/claude-code-context-pack-pg-spectrograph.md`](frozen/claude-code-context-pack-pg-spectrograph.md).
Full user-facing documentation (what's captured, what's provably not, how to verify
that yourself) lands at Milestone 12 per the spec's definition of done.

## Status

Under active development. See the plan's milestone roadmap for build order; nothing
here is installable or usable yet.

## Development

```bash
uv sync
uv run pytest
```
