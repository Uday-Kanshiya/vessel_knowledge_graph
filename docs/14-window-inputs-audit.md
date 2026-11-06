# Window inputs audit: what the model was given

> Status: **built, verified on a real re-run (2026-10-21)**
> Code: `src/extraction/window_update.py` (`WindowResult.prompt_text / attempts_log / record_summary`),
> `src/extraction/runner.py` (`inputs` block, `_attachment_inputs`, `_window_tokens`),
> `src/extraction/store.py` (`put_window_prompt` / `get_window_prompt`),
> `src/attachments/digest.py` (`AttachmentDigester.outcomes`), `src/site/views.py` (`window_inputs_view`),
> `src/site/templates/admin_journey.html.j2`. Tests: `tests/unit/test_pipeline_fake_llm.py` (2),
> `tests/unit/test_site_build.py` (3).

## 1. Why

`windows/wNNN.json` recorded what the model *returned* (`raw_response`, `applied`, `record_after`) but not what
it was *given*: the prompt was built in `run_window()` and discarded, and the only trace of the input was
`mail_ids`, `attachment_shas`, `prompt_chars`. The journey page could show the delta but not the evidence that
produced it. Now every window audit carries an `inputs` block and a `.prompt.txt` sidecar, and the journey page
shows them under "what the model saw" (12 §5.2).

Audit data only. The record (`record.json`) and the extractor's output schema are unchanged (working agreement:
fields describe data, not extraction statistics). Audit files written before this change have no `inputs`; they
still load, and the page says so.

## 2. Files

```
data/extractions/<casefile_id>/windows/
  wNNN.json          as before + "inputs": {...}            (this doc, §3)
  wNNN.prompt.txt    the exact user prompt of attempt 1 = render_window(record, mails), byte-identical
                     (the system prompt is not copied: inputs.prompt_version names it)
run_log.json  runs[].windows[]  as before + mails, attachments_sent, tokens   (summary view, §5)
```

The sidecar keeps the `.txt` suffix because `FileRecordSource.windows()` globs `w*.json`. A retried window's
attempt-2 prompt is attempt 1 plus `inputs.attempts[1].feedback`, so it is not stored twice.

## 3. `inputs` schema, exactly as stored

```
inputs:
  prompt_version   str     WINDOW_UPDATE_PROMPT_VERSION, e.g. "v5" (src/prompts/window_update/<v>.md)
  record_summary   dict    summary_for_prompt(record) as embedded in the prompt, BEFORE the delta was
                           applied: equipment[] problems[] faults[] action_logs[] (id + recognising
                           fields, no provenance) + next_free_ids
  next_free_ids    dict    record_summary.next_free_ids: {equipment: "e3", problems: "p1", faults: "f2",
                           action_logs: "a5"} — what the model would mint from
  mails            list    every page of the window, pagenum order, twins INCLUDED:
    mail_id             str
    pagenum             int
    date                str|null   page.date_time
    sender              str        "<name> <<email>>" as the prompt shows it
    to                  str|null
    subject             str|null
    context_only        bool       pagenum <= last processed (only with a non-zero overlap)
    body_chars          int        length of the body text as sent
    body_sha1           str        CasefileRunner._body_key(body): sha1 of the match-normalised text,
                                   the same key the twin detection uses
    fetched_now         bool       second value of mail_text(): HTML fetched in this window (else cache)
    skipped_as_twin_of  str|null   mail_id of the earlier mail with the same body; such a page was NOT
                                   shown to the model and has no attachments processed
    attachments         list       every URL on page.attachments, in order:
      url               str        the audit keeps the URL; the prompt never had it (07 §3)
      name              str        attachment_name(url)
      sha256            str|null   null on fetch_error / not_processed
      status            str        fetched_digested | cache_hit | junk_skipped | fetch_error | not_processed
                                     fetched_digested  URL not in the url cache: fetched, sha'd, digested now
                                     cache_hit         URL known; digest reused (digested_now says if rebuilt)
                                     junk_skipped      junk filter: not content-bearing (whatever the fetch path)
                                     fetch_error       fetch failed; `error` has the exception
                                     not_processed     page is a skipped twin, or attachment mode is off
      content_bearing   bool
      read_method       str|null   digest.extraction_method: pdf_text | pdf_ocr | docx_text | xlsx_table |
                                   eml_parse | vision | listed_only | skipped
      digested_now      bool       the digest doc was (re)built in this call
      error             str|null
      sent_to_model     bool       content_bearing and the page was not a twin
      digest_as_sent    dict|null  digest_for_prompt(doc) exactly as it went into the prompt, when sent
  tokens           dict    THIS window's model calls only (TokenTracker.calls sliced around run_window;
                           digest calls before it and split/translate calls after it are excluded):
                           {prompt_tokens, completion_tokens, total_tokens, call_count}
  attempts         list    one entry per attempt (a retried window keeps both):
    attempt          int
    prompt_chars     int    len(prompt sent) = len(prompt_text) + len(feedback)
    feedback         str    "" on attempt 1; the "## CORRECTION ..." text appended on attempt 2
    raw_response     any    the model's JSON for that attempt (attempt N == top-level raw_response)
    error            str|null  DeltaError text when the delta could not be applied, else null
```

`run_log.runs[].windows[]` entries gain `mails` (pages sent), `attachments_sent`, `tokens` (same dict).

## 4. Example: KMARIN RESOLUTION `6a372fc706f7689c0eefde1d`, w000, re-run 2026-10-21

Trimmed: the two `raw_response` copies are elided, the recipient list is cut. This casefile has no attachments;
an attachment row looks like the fixture in `tests/unit/test_site_build.py::_window_with_inputs`. The brief asked
for a KING ONE example; KING ONE is 44 windows and was not re-run for this (the smallest casefile was, as the
Verify step asked).

```json
"inputs": {
  "prompt_version": "v5",
  "record_summary": {"equipment": [], "problems": [], "faults": [], "action_logs": [],
                     "next_free_ids": {"equipment": "e1", "problems": "p1", "faults": "f1", "action_logs": "a1"}},
  "next_free_ids": {"equipment": "e1", "problems": "p1", "faults": "f1", "action_logs": "a1"},
  "mails": [
    {"mail_id": "6a372fa28a90ec15ea2b3cb9", "pagenum": 0, "date": "2026-06-21T00:26:05Z",
     "sender": "Cheng - KMarin Resolution <cheng.kmarinresolution@synergyship.com>",
     "to": "dalavaisivakumar@synergyship.com, joshua.w@synergyship.com",
     "subject": "Main engine OMD fault rectified", "context_only": false,
     "body_chars": 934, "body_sha1": "43c38389802ab88856b42f11d19d533da841c31c",
     "fetched_now": false, "skipped_as_twin_of": null, "attachments": []},
    {"mail_id": "6a37730ce37f165b026dc157", "pagenum": 1, "date": "2026-06-21T05:13:47Z",
     "sender": "Sivakumar Dalavai Venkatakrishnan <dalavaisivakumar@synergyship.com>",
     "to": "cheng.kmarinresolution@synergyship.com, kmarinresolution@syn…",
     "subject": "Main engine OMD fault rectified", "context_only": false,
     "body_chars": 1777, "body_sha1": "3bf5b6db085a7d9812375668507248bb458b4a77",
     "fetched_now": false, "skipped_as_twin_of": null, "attachments": []}
  ],
  "tokens": {"prompt_tokens": 5621, "completion_tokens": 2547, "total_tokens": 8168, "call_count": 1},
  "attempts": [
    {"attempt": 1, "prompt_chars": 3736, "feedback": "",
     "raw_response": "<same object as top-level raw_response>", "error": null}
  ]
}
```

`w000.prompt.txt` (3,875 bytes) starts `## RECORD SO FAR (summary with ids)`, then `## WINDOW` with
`### MAIL mail_id=… pagenum=0` and `pagenum=1`, then `## RESPOND`. `fetched_now` is false on both mails because
`--restart` discards the record, not the mail cache.

## 5. Sizes (decision input for a `--no-prompt-text` flag)

| file | before | after |
|---|---|---|
| `w000.json` (2 mails, no attachments, 1 attempt) | 10,907 B | 18,363 B |
| `w000.prompt.txt` | — | 3,875 B |

Of the 4.5 KB `inputs` block, 3.2 KB is `attempts[].raw_response`, a copy of the top-level `raw_response`
(kept by design so a retried window has both responses); `mails` is 0.9 KB. The prompt sidecar is a third of
the JSON and holds nothing that is not already in the mail cache plus `record_summary`, so it is not the file
to flag off. On KING ONE's 44 existing windows (avg 27.5 KB each, written before this change) the growth
driver will be `digest_as_sent.text_excerpt` (up to 4,000 chars per sent attachment, already present in the
prompt). If size matters there, the lever is a `--no-digest-text` that nulls `text_excerpt` in
`digest_as_sent` (the sha still links to the attachment page), not a `--no-prompt-text`.

## 6. On the journey page (12 §5.2)

Under each window row, collapsed `what the model saw`: memory line (`e N · p N · f N · a N`, next free ids,
prompt version) · mails table (page, date, from, subject, body chars, tags `context only` / `twin of …` /
`fetched now` / `N att`, mail link when the page exists; twins greyed) · attachments table (name, sha link when
the digest page exists, status chip, read method, sent ✓/–, digest summary on hover) · tokens in/out, calls,
sent counts, attempt count · `correction sent before attempt N` · `raw prompt (attempt 1, Nk chars)` ·
`raw response · attempt N` as pretty JSON. Windows without `inputs`: *inputs not captured — audit written by
an earlier extraction run; re-run to get them*. Casefile and mail pages are unchanged; `site.css` only.
