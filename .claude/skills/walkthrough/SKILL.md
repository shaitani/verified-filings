---
name: walkthrough
description: Run evals/walkthrough.py over every eval question and leave a fresh, complete data/question-walkthrough.md. Use when the user asks to walk through all the questions or regenerate the question walkthrough. Runs unattended to completion - never pauses for input.
---

# Walk through every eval question

Invoking this skill **is** the explicit request for a whole-set run (the usual
"no full eval runs unasked" rule is satisfied). The last full run (2026-09-25) took 9m27s for 55 questions.

## Ground rules

- **Do not ask the user anything and do not stop early.** No AskUserQuestion,
  no "should I continue?", no waiting. Every problem below has a prescribed
  response; if something unforeseen happens, pick the most sensible fix,
  note it, and keep going.
- Do not edit app code, questions, or the scorer to make things pass. This is a
  measurement, not a fix. The only files written are the walkthrough output
  and scratch logs.
- Do not commit.

## Steps

1. **Preflight** (fix, don't ask):
   - `docker ps` must show `verified-filings-db-1` and `verified-filings-ollama-1`
     healthy. If either is down: `docker compose up -d db ollama` and wait for
     healthy (poll with a Monitor until-loop, not sleep).
   - Run from the repo root: `C:\claude\verified-filings`.

2. **Run it in the background** so the tool timeout can't kill it:

   ```bash
   cd /c/claude/verified-filings && uv run python -m evals.walkthrough > "$SCRATCH/walkthrough.log" 2>&1
   ```

   with `run_in_background: true` (`$SCRATCH` = the session scratchpad dir).
   Output goes to `data/question-walkthrough.md`, rewritten after each
   question. Wait for the completion notification; don't poll in a tight loop.

3. **If the process dies before the last question** (check the log tail and
   the last `## qNNN` heading in the markdown against the last id in
   `evals/questions.yaml`):
   - Fix the environmental cause if there is one (container down -> restart it).
   - Re-run the remaining range to a separate file:
     `uv run python -m evals.walkthrough <next_id> <last_id> --out "$SCRATCH/rest.md"`
   - Splice the remaining question sections onto the main file so it covers
     every question, and recompute any summary/tally at the top from the
     per-question grades. Repeat until every question id is present.
   - A single question that crashes the whole process twice: run the ranges
     either side of it, and add a section for it recording the crash verbatim
     (grade it as a crash). Never let one question block the rest.

4. **Verify**: every id in `evals/questions.yaml` has a section in
   `data/question-walkthrough.md`, and the file's timestamp is from this run. The log's last lines carry a `TALLY` summary; report run time as log file creation to last write.

5. **Report** (briefly): tally by grade as printed on the TALLY line (pass / fail / unsafe / gap),
   the ids graded `unsafe` and `fail`, any questions that crashed, and any
   recovery steps taken. Link the file. `unsafe` is the grade to lead with.
