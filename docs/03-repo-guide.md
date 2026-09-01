# Vessel Knowledge Graph — Repo Guide

> Status: **conventions, agreed**
> Date: 2026-09-01
> Read `01-problem-statement.md` (why) and `02-design-and-plan.md` (what) first.
> This document is **where things go and how we keep them that way**.

The layout below follows `multi_agent_rag_offline/qhsc_mail_process`, which is
already in production. Anyone who has worked on that repo should be able to find
their way around this one without asking. Where we deviate, the deviation is
called out and justified.

---

## 1. The rule that matters most

**Create a folder when you have a second file to put in it, not before.**

An empty `src/search/` with a stub in it is a lie about how far the project has
got. This guide describes the *target* shape so that when you do add a file you
know where it belongs — it is not a checklist to build out up front.

Today the repo is `docs/` only. Phase 0 (pilot) will add `src/config/`,
`src/db/`, `src/extraction/`, `src/prompts/` and `tests/`. Nothing else should
appear until the phase that needs it.

---

## 2. Target layout

```
vessel_knowledge_graph/
├── docs/                     # numbered, ordered, one topic each
│   ├── 01-problem-statement.md
│   ├── 02-design-and-plan.md
│   ├── 03-repo-guide.md      # this file
│   └── _superseded/          # history — never deleted, never referenced
│
├── schema/
│   └── casefact.v1.schema.json   # THE contract (02 §2.5). See §6 below.
│
├── src/
│   ├── config/               # every env var in the repo is read here
│   ├── db/                   # Mongo connection pool + index definitions
│   ├── llm/                  # provider client, retries, token accounting
│   │
│   ├── corpus/               # PART 1 boundary: which casefiles are defects
│   ├── attachments/          # 02 §2.3 — cache, junk filter, typed readers
│   ├── extraction/           # 02 §2.1–2.4 — windowing, state, consolidate
│   ├── contract/             # CaseFact models + the deterministic validator
│   │
│   ├── graph/                # PART 3 — loader, store adapter, aggregates
│   ├── resolution/           # 02 §3.9 — alias table, resolver cascade, ontology
│   ├── search/               # 02 §4 — scope, query path, back-off, stats
│   │
│   ├── prompts/<name>/vN.md  # immutable, versioned. See §7.
│   ├── pipelines/            # the long-running entrypoints (`python -m ...`)
│   └── utils/                # observability, alerts, token logger
│
├── eval/                     # 02 §5 — gold set + metric scripts. See §9.
├── tests/                    # unit/ and integration/. See §8.
├── scripts/                  # one-off and operational scripts. See §10.
│
├── k8s/                      # configmap, deployment, secret.yaml.example
├── .github/workflows/        # build → ECR → EKS
├── Dockerfile
├── pyproject.toml            # deps live here; requirements.txt is generated
├── .env.example              # every var, commented. Never a real .env.
├── VERSION  ·  CHANGELOG.md  # one semantic version for the whole repo
└── README.md                 # 20 lines: what it is, how to run it, where docs are
```

### Why the src/ subfolders are these ones

They are the parts of `02-design-and-plan.md`, in order. `corpus → attachments →
extraction → contract` is Part 2; `graph → resolution → search` is Part 3. The
boundary between them is `schema/` and `src/contract/`, and **that boundary is
one-directional**: extraction code may not import from `src/graph/`, and graph
code may not import from `src/extraction/`. If you find yourself wanting to,
the thing you want belongs in the contract instead — raise it, don't reach
across.

---

## 3. Configuration

**One rule: every environment variable in this repo is read in
`src/config/__init__.py` and nowhere else.** Modules import named constants.
Nobody calls `os.environ` outside that file.

This is what makes `.env.example` and `k8s/configmap.yaml` trustworthy as
documentation. If a var is not in `src/config`, it does not exist.

The pattern (lifted from qhse, keep it):

```python
def _req(key: str) -> str:          # required — fails at import, not in prod at 2am
def _env(key, default="")           # string
def _env_int(key, default)          # warns and falls back on a bad value
def _env_float(key, default)
def _env_bool(key, default)
def _env_list(key, default="")      # comma-separated
```

**When you add a var, you touch four files in the same PR:**

| File | What goes in |
|---|---|
| `src/config/__init__.py` | the constant + a comment saying what it controls |
| `.env.example` | the var with a safe placeholder and the same comment |
| `k8s/configmap.yaml` | if it is **not** a secret |
| `k8s/secret.yaml.example` | if it **is** a secret (never `secret.yaml`) |

A PR adding a config constant without the `.env.example` line will be asked to
add it. That file is how a new person gets running.

### Things that must be config, not literals

Thresholds from `02` are tuning knobs and will be tuned: `WINDOW_STRIDE`,
`WINDOW_SIZE`, `MAX_STEPS_PER_BLOB`, `PDF_OCR_PAGE_THRESHOLD`,
`MIN_SUPPORT_CASES`, `MIN_SUPPORT_VESSELS`, `STALE_AFTER_DAYS` (6 months),
`EMBEDDING_MATCH_THRESHOLD` (0.85). If a number in the design doc has a
justification attached, it is config.

### Secrets

`.env` and `k8s/secret.yaml` are gitignored. `.env.example` and
`k8s/secret.yaml.example` are committed. `.dockerignore` must exclude `.env`
and `k8s/` — both Dockerfiles do `COPY . /app/`, so without it credentials get
baked into image layers. This has bitten the qhse repo's ancestors; do not
rediscover it.

---

## 4. MongoDB

### Three logical connections, never one

```
source      read-only   casefiles, mails, machinery, vessels
facts       read/write  CaseFacts, attachment digests, checkpoints, queues
token_log   write-only  LLM token accounting
```

`source` and `facts` are separate URIs **as a safety boundary, not for
performance**. `02 §4.4` says we annotate in our own layer and never write back
to the source system. Separate credentials make that enforceable instead of
merely intended. The source connection should be issued a read-only user.

Use one lazily-initialised singleton pool (`src/db/__init__.py`), same shape as
qhse: an async client per service via `motor`, plus sync clients for scripts.
Pool settings, timeouts and `close_all_connections()` copy over unchanged.

### The fact store is rebuildable — treat it that way

Everything we write can be regenerated from source by re-running extraction.
That has two consequences:

- **Index definitions live in code** (`src/db/indexes.py`), applied idempotently
  at startup, not in a migration tool. Drop the DB, re-run, get the indexes back.
- **Never store the only copy of anything there.** Hand annotations for the gold
  set live in `eval/`, in git.

Indexes you will need on day one:

```
case_facts          (casefile_id, blob_id, extraction_version)  UNIQUE
attachment_digests  (sha256)                                    UNIQUE
extraction_checkpoints (worker)                                 UNIQUE
new_term_queue      (term, node_type)                           UNIQUE
<health log>        (created_at)  TTL = HEALTH_LOG_TTL_DAYS
```

The `case_facts` unique key **includes `extraction_version`** on purpose: a
re-run with a better prompt must neither duplicate the old record nor destroy
it, because comparing versions is how we measure that the prompt improved.

---

## 5. Polling

qhse polls a mail collection with a `START_DATETIME` env var and an `isDiary`
flag written back onto the source document. **We cannot do the second half of
that** — we have no write access to source, by design (§4). So:

**Watermark in our own checkpoint collection, not a flag on the source doc.**

```
extraction_checkpoints
  { worker: "extraction",
    watermark: <datetime>,        # highest source updatedAt fully processed
    extraction_version: "1.0.0",
    updated_at: <datetime> }
```

The loop, mirroring `qhse_live_processor` in structure:

```
resolve watermark  (checkpoint, else CORPUS_START_DATETIME on cold start)
loop:
    health check dependencies — back off, don't crash, on failure
    claim a batch:  defect casefiles with updatedAt in (watermark, now],
                    sorted ascending, limit EXTRACTION_BATCH_SIZE
    process with bounded concurrency (EXTRACTION_CONCURRENCY)
    advance watermark to the last fully-processed timestamp, minus a small
      overlap — re-processing a casefile is idempotent (unique key), missing
      one is not recoverable without a manual backfill
    sleep to fill EXTRACTION_CYCLE_INTERVAL
```

Conventions that come with it:

- **Historical mode and live mode are the same code**, differing only in window
  size — qhse splits historical into `TIME_BATCH_HOURS` chunks and then falls
  through to a live loop. Copy that; a four-year backfill is the normal case here,
  not the exception.
- **Overlap on the watermark, never a gap.** Idempotency is the unique index's
  job.
- **A casefile is re-processed when its `updatedAt` moves**, because a case that
  gets three new mails needs its CaseFact updated. Extraction must therefore be
  re-runnable over a whole casefile from scratch — no incremental state outside
  the CaseFact itself.
- **Health check before each batch**, not just at startup. A pod that keeps
  looping against a dead Mongo while reporting healthy is worse than a crash.
- **Print a cycle banner with counts.** Ops reads logs, not metrics dashboards.

---

## 6. The contract

`schema/casefact.v1.schema.json` is the frozen JSON Schema from `02 §2.5`, and
`src/contract/` holds the Pydantic models plus the deterministic validator.

Rules:

1. **The JSON Schema is the source of truth**, not the Pydantic models. The
   models are generated from or checked against it in a test.
2. **Additive changes only within a major version.** A new optional field is
   `v1.1`; removing or retyping a field is `v2`, a new schema file, and a
   coordinated change on both sides.
3. **`extraction_version` is stamped on every record** and is bumped whenever
   the prompt or the schema changes. It is the only way to answer "which code
   produced this?" six months from now.
4. **The validator is deterministic — no model calls.** Schema validity,
   resolvable `mail_id`s, and the evidence-quote substring check (`02 §2.4`,
   target fabrication rate 0). It emits `validation_flags[]`, it does not throw.
   Flagged records still load, tagged `low_confidence`; excluding them biases
   the corpus toward easy cases.

---

## 7. Prompts

`src/prompts/<name>/v1.md`, `v2.md`, … — **immutable files, one per version.**
Never edit a prompt in place. A change is a new `vN.md` plus a config flip
(`WINDOW_UPDATE_PROMPT_VERSION=v2`) and a restart. No image rebuild, and a
regression is a one-line rollback.

Prompts are loaded through a single helper in `src/config`, so the version in
use is always visible in one place:

```python
get_prompt("window_update", WINDOW_UPDATE_PROMPT_VERSION)
```

Expect at minimum: `window_update/`, `consolidate/`, `attachment_digest/`.

`.dockerignore` must **not** blanket-ignore `*.md` — these are runtime files.

---

## 8. Tests

```
tests/
├── conftest.py          # fixtures: sample casefile, sample CaseFact, fake LLM
├── fixtures/            # committed JSON — the OASIS casefile lives here
├── unit/                # no network, no DB, no LLM. Runs in CI on every push.
└── integration/         # real Mongo / LLM / S3. Marked, skipped without creds.
```

- `pytest -m "not integration"` must pass on a laptop with **no `.env` at all**.
  That is the bar; if a unit test needs credentials, it is an integration test.
- Mark integration tests with `@pytest.mark.integration` and skip cleanly when
  the credential is absent — never fail.
- **The deterministic parts get real tests**, and they are the ones that matter:
  the junk filter's drop decisions, the evidence-quote substring check, blob
  status precedence, watermark advancement, the false-loop collapse rule.
- **LLM output is not unit-tested for content** — it is measured in `eval/`.
  Test the code around the call: prompt assembly, JSON parsing, the
  dropped-object assertion (`02 §2.2` rule 1), retry behaviour.
- Note: qhse's `tests/` are partly `asyncio.run(main())` demo scripts rather
  than assertions. **Do not copy that.** Demo scripts go in `scripts/`; `tests/`
  contains assertions that CI can fail on.

---

## 9. Evaluation

`eval/` is not an afterthought folder — `02 §5` says start it first.

```
eval/
├── gold/          # 50–100 hand-annotated cases, committed JSON, in git
├── metrics/       # one script per metric row in 02 §5
└── README.md      # how a superintendent's annotation becomes a gold file
```

The gold annotations are **the most expensive artefact in this repo** — they
cost superintendent time we had to book. They live in git, reviewed like code,
and are never regenerated by a model. The headline metric is the dead-end
warning rate; whatever else moves, that one gets reported every phase.

---

## 10. Scripts

`scripts/` is for things run by a human, not by the pipeline: backfills,
one-off diagnostics, cache warming, ontology exports, "why did casefile X come
out like this" investigations.

- Every script takes `--dry-run` and defaults to it if it writes anything.
- Every script prints what it *would* do before doing it.
- Anything that survives its third use gets promoted into `src/` with a test.
- Scripts may import from `src/`; nothing in `src/` may import from `scripts/`.

qhse has both `scripts/` and `src/scripts/` and it is not obvious which is
which. **We use one: `scripts/` at the root.**

---

## 11. Naming

| Thing | Convention | Example |
|---|---|---|
| Modules, functions | `snake_case` | `junk_filter.py`, `advance_watermark()` |
| Classes | `PascalCase` | `CaseFact`, `WindowExtractor` |
| Config constants | `SCREAMING_SNAKE` | `MAX_STEPS_PER_BLOB` |
| Mongo collections | `snake_case`, plural | `case_facts`, `attachment_digests` |
| Graph node labels | `PascalCase`, singular | `Symptom`, `EquipmentInstance` |
| Graph edge types | `SCREAMING_SNAKE`, verb | `OCCURS_ON`, `FOLLOWED_BY` |
| Docs | `NN-kebab-case.md` | `03-repo-guide.md` |
| Prompts | `<stage>/vN.md` | `window_update/v2.md` |

Use the vocabulary from `02` exactly — `Problem` not `RootCause`, `Gate` not
`Blocker`, `step` in extraction and `Action` in the graph. When code and doc
disagree on a word, one of them is a bug.

---

## 12. Working together

- **Branch off `main`, PR into `main`.** Small PRs; a PR that touches both
  extraction and graph is usually two PRs.
- **The docs are part of the deliverable.** A design decision that changes
  `02-design-and-plan.md` updates that file in the same PR, including the
  decisions log. Superseded documents move to `docs/_superseded/` — they are
  never deleted and never linked from current docs.
- **Open questions live in `02 §8`, not in code comments.** If you resolve one,
  move it to the decisions log with the reason.
- **`CHANGELOG.md` under `[Unreleased]` as you go**; `VERSION` is bumped once per
  release to production, not per commit. One version for the whole repo.
- **Comments explain why, not what.** The qhse config file is the model here:
  every non-obvious default carries the reason it is that number. Do that,
  especially for anything the pilot will want to change.

---

## 13. Bootstrap order

For whoever starts phase 0, in this order and no further:

1. `pyproject.toml`, `.gitignore`, `.env.example`, `README.md`
2. `src/config/__init__.py` — the constants phase 0 actually needs
3. `src/db/__init__.py` + `src/db/indexes.py`
4. `tests/conftest.py` + the OASIS casefile in `tests/fixtures/`
5. `src/corpus/` — defect selection and the checkpoint/watermark
6. `src/attachments/` — tier 1 junk filter first; it is the cost lever, and it
   is deterministic so it is testable before any model call exists
7. `src/extraction/` + `src/prompts/window_update/v1.md`
8. `schema/casefact.v1.schema.json` + `src/contract/`

Stop there. The phase 0 exit gate in `02 §6` is a judgement about OASIS output,
not a line count — if the pilot says *"renewed valve, resolved"*, the next
commit fixes the prompt, not the folder structure.
