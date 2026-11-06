# vessel_knowledge_graph

Defect casefiles (mail threads + attachments) → structured, cited facts → a fleet-wide knowledge graph
that answers "what has historically worked, and what has been a dead end" for a live machinery problem.

**Where things are**

- `docs/` — numbered design docs; `docs/README.md` is the index. Start with 01 (why), 02 (what), 03 (repo
  conventions); 04–08 are the extraction schema decisions and the pipeline as it now stands.
- `schema/` — the two extraction contracts (`casefile_extract.v2`, `attachment_digest.v1`) with KING ONE
  samples; `schema/README.md` explains what changed from the earlier schemas and why.
- `docs/08-processing-pipeline.md` / `.drawio` — the end-to-end order of operations.
- `scripts/build_site.py` → `data/site/` — the static casefile browser over the extracted records (docs/12):
  search, casefile pages with cited quotes, admin lookup tables / review queue / build journey.
- `src/` — config, LLM client, observability; `corpus/` (export loader), `attachments/` (fetch, sha cache, junk
  filter, readers, digest), `extraction/` (5-mail window loop, record merge, consolidate, run store),
  `contract/` (deterministic validator), `prompts/` (versioned).
- `scripts/extract_casefiles.py` — runs the v2 extraction over the test set through the LLM API; dry-run by
  default. `docs/10-extraction-run-guide.md` has the controls.
- `docs/11-extraction-changes.md` — what was built and what changed after each review of the output.

**Run**: copy `.env.example` to `.env` (LLM key, `CASEFILE_EXPORT_PATH`), `uv sync --extra attachments`,
`pytest -m "not integration"`, then `python scripts/extract_casefiles.py --list` and add `--run` with the
selection you want (`--ids`, `--limit`, `--max-windows`).
