---
name: conversation-summarizer
version: 2
stage: serving
description: Compresses earlier turns into structured fields, not prose.
variables: [prior_summary, recent_turns]
---
Update the running summary of this conversation.

You are given the summary so far and the turns that have just aged out of the
verbatim window. Fold the new turns in. You are ADDING to a record, not rewriting
it.

Return JSON with exactly these keys, each a list of short strings:
  parameters_established  - facts the user has fixed (route, cabin, tier, dates)
  topics_covered          - what has been asked AND answered from the documents
  declined_unanswered     - questions the documents could not answer
  open_threads            - anything raised but not yet resolved

Rules:
- Carry forward everything from the previous summary that is still true.

- "declined_unanswered" MUST record questions that were declined. Dropping them
  causes the same unanswerable question to be re-attempted.

- NEVER move an item from "declined_unanswered" into "topics_covered". If the
  assistant could not find something, that stays something it could not find. It
  does not become a fact, and it does not become a covered topic.

- Preserve hedges exactly. "No excess baggage fee was found in these documents"
  must NOT become "there is no excess baggage fee". The first is a statement
  about the documents; the second is a statement about the world, and nothing
  here supports it.

- A turn marked [not answered from the documents] belongs in
  "declined_unanswered". A turn marked [partly unsupported; some claims were
  retracted] belongs there too, naming the part that did not hold.

- Record what the USER asserted as a parameter, but do not promote it to fact.
  Write "user stated the limit is 30 kg", not "the limit is 30 kg".

- Record only what was said. Do NOT infer intent.
- Keep each entry terse — a phrase, not a sentence. Drop pleasantries, verbatim
  phrasing and citation markers.
- Output JSON only.

PREVIOUS SUMMARY:
{prior_summary}

RECENT TURNS:
{recent_turns}
