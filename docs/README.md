# Documentation index

This is the operator and maintainer handoff for the Sansad PDF pipeline. The
top-level README is the quick start; these documents explain the state,
decisions, failure modes, and boundaries behind it.

| Document | Purpose |
|---|---|
| [Architecture](ARCHITECTURE.md) | Stages, trust rules, components, and artifacts |
| [Operations](OPERATIONS.md) | Commands, monitoring, retries, backup, and recovery |
| [Command reference](COMMAND_REFERENCE.md) | Every CLI command, option, default, and exit behavior |
| [Configuration](CONFIGURATION.md) | Every TOML setting and environment variable |
| [Data model](DATA_MODEL.md) | SQLite tables, statuses, and export semantics |
| [Sources and coverage](SOURCES_AND_COVERAGE.md) | Endpoints, census results, gaps, and scope |
| [Quality and testing](QUALITY_AND_TESTING.md) | Routing, validation, evidence, and release gates |
| [Cloud pipeline](CLOUD_PIPELINE.md) | GitHub Actions compute, Hugging Face storage, Space Bunny, resume, and accuracy policy |
| [Roadmap](ROADMAP.md) | Work explicitly not implemented yet |

Counts dated 2026-08-01 are snapshots, not timeless claims. Secrets are never
documentation: `.env` must not be copied into reports, logs, or bug reports.
