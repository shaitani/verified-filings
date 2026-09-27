---
name: walkthrough
description: Run evals/walkthrough.py over every eval question and leave a fresh, complete data/question-walkthrough.md. Use when the user asks to walk through all the questions or regenerate the question walkthrough. Runs to completion in its own console window with a live status line - never pauses for input.
---

# Walk through every eval question

Invoking this skill **is** the explicit request for a whole-set run (the usual
"no full eval runs unasked" rule is satisfied). It takes about 10 minutes.

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

2. **Run it in its own console window** so the user can watch it live. On a
   terminal the script shows one status line that updates in place (question,
   time on it, total time, pass/fail so far); `--log` gets the per-question
   lines and the summary for you to read. `$S` = the session scratchpad dir.

   ```powershell
   Remove-Item "$S\walkthrough.log" -ErrorAction SilentlyContinue
   Start-Process -FilePath powershell.exe -WorkingDirectory "C:\claude\verified-filings" -ArgumentList @('-Command', "`$host.UI.RawUI.WindowTitle = 'walkthrough'; uv run python -m evals.walkthrough --log '$S\walkthrough.log'")
   ```

   Then wait for it with a **background** Bash until-loop (`run_in_background:
   true`), which notifies you once when the run ends:

   ```bash
   L="$S/walkthrough.log"; for i in $(seq 1 720); do grep -qE "^(written:|ABORTED)" "$L" 2>/dev/null && break; [ -f "$L" ] && [ $(( $(date +%s) - $(stat -c %Y "$L") )) -gt 600 ] && { echo STALLED; break; }; sleep 5; done; cat "$L"
   ```

   `written:` = finished. `ABORTED` = the script died or was interrupted.
   `STALLED` (log untouched for 10 min) = the window was closed or the
   process hung. Treat the last two as step 3. Output goes to
   `data/question-walkthrough.md`, rewritten after each question. The window
   closes itself when the script exits, so the chat is where the result is
   seen (step 5).

3. **If the process dies before the last question** (check the log tail and
   the last `## qNNN` heading in the markdown against the last id in
   `evals/questions.yaml`):
   - Fix the environmental cause if there is one (container down -> restart it).
   - Re-run the remaining range to a separate file:
     `uv run python -m evals.walkthrough <next_id> <last_id> --out "$S/rest.md"`
   - Splice the remaining question sections onto the main file so it covers
     every question, and recompute any summary/tally at the top from the
     per-question grades. Repeat until every question id is present.
   - A single question that crashes the whole process twice: run the ranges
     either side of it, and add a section for it recording the crash verbatim
     (grade it as a crash). Never let one question block the rest.

4. **Verify**: every id in `evals/questions.yaml` has a section in
   `data/question-walkthrough.md`, and the file's timestamp is from this run. The log's last lines carry `TOTAL TIME` and a `TALLY` summary. Each question's
   section ends with `**Time:**`, and the log has a `qNNN <grade> in N.Ns` line per question.

5. **Report** (briefly). Open with the log's `FINAL` line verbatim in a code
   block -- it is the live status line's last state. Then: tally by grade as printed on the TALLY line (pass / fail / unsafe / gap),
   the ids graded `unsafe` and `fail`, any questions that crashed, and any
   recovery steps taken. Give the total time and the five slowest questions, with their parse / map / answer split. Link the file. `unsafe` is the grade to lead with.
