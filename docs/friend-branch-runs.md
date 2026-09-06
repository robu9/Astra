# Friend-branch run comparison (6 Sep 2026)

Archive of the four GitHub branches that existed before `main` was reset to empty memory. Live artefacts under `memory/` and `runs/` were wiped after this was written; this file is the record.

**Chart (same as the comparison canvas):** open [`scoreboard/friend-runs.html`](../scoreboard/friend-runs.html) in a browser, or `ao preview scoreboard/friend-runs.html`. Numbers are in [`docs/friend-branch-runs.json`](friend-branch-runs.json). To rebuild the page after editing the JSON:

```bash
python -m astra.friend_board
```

Branches at the time:

| Branch | Author | What it was |
| --- | --- | --- |
| `main` | Punay04 (HEAD) | Original learning session already on default |
| `learn/ao-check-runs` | robu9 | Fresh session from empty memory (CRM + tracker + live GitHub stale PRs) |
| `learn/crm_at_risk` | Punay04 | Five CRM retests on **already-trained** memory from `main` |
| `learn/tracker_triage` | Punay04 | Five tracker retests on **already-trained** memory from `main` |

Source: each branch’s `runs/results.tsv` plus traces/reflections on the two Punay04 learn branches.

---

## Verdict

- **CRM learned.** Punay’s CRM retests were 1.00 on four of five seeds. The one 0.00 was a JSON-envelope bug, then r05 recovered and the miss was reverted.
- **Tracker did not keep anything.** All five Punay tracker runs were reverted because `main` already had a 1.00 tracker episode. Behaviourally: skipped `list_comments` (duplicates), and on r02 never called `add_label` / `assign_issue`.
- **`learn/ao-check-runs` (fresh memory):** CRM 0.00 → 0.92 (peaked at 1.00). Tracker 0.64 → 0.65 net. Stale PRs 0.00 → 0.05.

These were not four copies of the same experiment. Punay’s two learn branches started from trained memory in `skilled` mode.

---

## `learn/ao-check-runs` — fresh memory, sequential

### CRM — yes, then it plateaued near-perfect

| Run | Quality | Vs previous | Improved? | Gate | What happened |
| --- | --- | --- | --- | --- | --- |
| r01 novel | 0.00 | — | baseline | n/a | Missed every at-risk signal |
| r02 recovering | 0.67 | +0.67 | Yes | kept | Found most deals; still skipped some notes |
| r03 skilled | 1.00 | +0.33 | Yes — perfect | kept | Skill promoted |
| r04 skilled | 0.93 | −0.07 | No — tiny dip | none | Missed one deal whose risk was only in notes |
| r05 skilled | 0.92 | −0.02 | No — held high | kept | Missed one unrecognized note signal |

Net: **0.00 → 0.92**. The jump is r01→r03.

### Tracker — one small gain, then two regressions

| Run | Quality | Vs previous | Improved? | Gate | What happened |
| --- | --- | --- | --- | --- | --- |
| r01 novel | 0.64 | — | baseline | n/a | Missed labels/assignees on several issues |
| r02 skilled | 0.71 | +0.07 | Yes, small | kept | Still skipped write tools on some issues |
| r03 skilled | 0.64 | −0.07 | No | reverted | Wrong priority labels and two wrong owners |
| r04 skilled | 0.49 | −0.15 | No | reverted | Misread “data loss” / login-outage priority |
| r05 skilled | 0.65 | +0.16 vs r04 | Yes vs r04, flat vs start | kept | Same priority-rule mistakes |

Net: **0.64 → 0.65**. Keep/revert correctly threw away r03 and r04.

### GitHub stale PRs — barely moved

| Run | Quality | Vs previous | Improved? | Gate | What happened |
| --- | --- | --- | --- | --- | --- |
| r01 novel | 0.00 | — | baseline | n/a | Called `search_issues` then ignored the payload |
| r02 recovering | 0.10 | +0.10 | Yes, tiny | kept | Still did not fetch reviewers or blockers |
| r03 recovering | 0.05 | −0.05 | No | kept | Returned an empty list after a correct search |

---

## `learn/crm_at_risk` — Punay04, trained memory, seeds 1–5

Five commits on top of `main`. Memory already knew the CRM rules (original session ended at 0.79, plus a later mock run at 1.00).

| Run | Quality | Vs previous | Vs original same seed | After this run? | Gate |
| --- | --- | --- | --- | --- | --- |
| r01 skilled | 1.00 | held at 1.00 | 0.00 → 1.00 | Held (already perfect) | kept |
| r02 skilled | 1.00 | 0.00 | 0.17 → 1.00 | No change — still perfect | kept |
| r03 skilled | 1.00 | 0.00 | 0.63 → 1.00 | No change — still perfect | kept |
| r04 skilled | 0.00 | −1.00 | 0.63 → 0.00 | Collapsed | reverted |
| r05 skilled | 1.00 | +1.00 | 0.79 → 1.00 | Recovered after revert | kept |

**What actually went wrong on r04:** not a missing CRM rule. The actor paginated deals, listed owners, pulled notes, and drafted a real `at_risk` list (`d_0404` PO pending, `d_0407` health R, …). Then it emitted raw `{ "at_risk": [...] }` instead of `{ "thought": "...", "final": { ... } }`. Two `malformed_replies`; stored final was `{ "thought": "" }`; grader saw 0/6 deals. It also never fetched notes for `d_0411` and `d_0417`. r05 scored 1.00; r04 was reverted.

---

## `learn/tracker_triage` — Punay04, trained memory, seeds 1–5

The grader scores **server state** after `add_label` / `assign_issue`, not the `triaged` JSON. Comments live in `list_comments`; ~20% of issues are secretly `duplicate of #N` (label `duplicate`, no assignee). `main` already had a tracker episode at 1.00, so anything below that was reverted.

| Run | Quality | Vs previous | Vs original same seed | After this run? | Gate |
| --- | --- | --- | --- | --- | --- |
| r01 skilled | 0.89 | 1.00 → 0.89 | 0.04 → 0.89 | No vs last episode | reverted |
| r02 skilled | 0.09 | −0.80 | 0.71 → 0.09 | Collapsed | reverted |
| r03 skilled | 0.67 | +0.58 vs r02 | 0.71 → 0.67 | Yes vs r02, still below bar | reverted |
| r04 skilled | 0.76 | +0.09 | 0.71 → 0.76 | Yes vs r03, still below bar | reverted |
| r05 skilled | 0.82 | +0.06 | 0.78 → 0.82 | Yes vs r04, still below bar | reverted |

### What actually went wrong

**r01 (0.89)** — 8/9 labels and assignees right. Never opened comments. **#106** `[exports] Production outage: exports service down for all users` got `sev-1` + Nora (`m_01`, owns exports). Keyword-wise that is correct unless a comment says duplicate — which is why it was wrong.

**r02 (0.09) — the collapse.** Four calls: list issues, members, labels, then `final`. The thought says it will call `add_label` and `assign_issue`. It never does. JSON report looks populated; server is empty (`labels=[]`, `assignee=None`). 0/9 labels, 2/9 assignees.

**r03 (0.67)** — wrote labels this time, still almost never read comments. **#305 / #308** (typos) and **#309** (500) look like `sev-3` / `sev-2` + owner. All three were duplicates.

**r04 (0.76)**
- **#403** `[mobile] Data loss after mobile migration` — never labeled (it did sibling #408).
- **#402 / #405** “All users cannot log in…” labeled `sev-2`. Hidden rule: that phrase is **sev-1**.

**r05 (0.82)**
- **#506 / #508** (empty-state copy): thought claims “I successfully added sev-3”, then only calls `assign_issue`. Labels on the server stayed `[]`.
- **#510** labeled `sev-2` and assigned Nora; grader marked it wrong (almost certainly an unread duplicate). Reflector then guessed sev-1 — wrong diagnosis.
- Hit `malformed_replies` at the end.

After r02 the curve climbs 0.09 → 0.67 → 0.76 → 0.82, but none of it was kept.

---

## `main` — original session (baseline)

### CRM on main

| r01 | r02 | r03 | r04 | r05 |
| --- | --- | --- | --- | --- |
| 0.00 | 0.17 | 0.63 | 0.63 | 0.79 |

r01→r02 yes, r02→r03 yes, r03→r04 held, r04→r05 yes. Net **0.00 → 0.79**.

### Tracker on main

| r01 | r02 | r03 | r04 | r05 |
| --- | --- | --- | --- | --- |
| 0.04 | 0.71 | 0.71 | 0.71 | 0.78 |

r01→r02 yes, r02–r04 held, r04→r05 yes. Net **0.04 → 0.78**.
