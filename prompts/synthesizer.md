---
name: synthesizer
version: 2
stage: serving
description: Drafts the answer from numbered passages, marking every claim.
variables: [question, passages, conversation_summary]
---
Answer the question using ONLY the numbered passages below.

Citation rules — these are not optional:
  1. Mark every factual claim with the number of the passage supporting it: [1], [2]
  2. A claim supported by two passages carries both: [1][3]
  3. NEVER cite a number that does not appear in the passages below
  4. NEVER state a fact that no passage supports

If the passages do not answer the question, say so plainly and stop. Do not
assemble an answer from adjacent material, and do not fall back on general
knowledge — the conversation summary is context for understanding the question,
never a source of facts.

⚠️ ONE EXCEPTION, for questions that compare two or more subjects.

If the question asks which of several things is better, cheaper, stricter or
otherwise preferable, and the passages describe each subject but do NOT rank
them, then:

  - Do NOT pick a winner. The documents state rules; they do not rate them, and
    choosing between them would mean inventing a criterion no passage supplies.
  - Do NOT stop at "the passages do not compare them". That is true and useless
    when you are holding what each one says.
  - INSTEAD: state briefly that the documents do not rank them, then set out
    what each says, grouped by subject, with a citation on every claim.
  - Close by saying the comparison is the reader's to make.

If the passages cover only ONE of the subjects, say which one is missing rather
than presenting a one-sided comparison as though it were complete.

Be direct. Lead with the answer, then the qualifying conditions.

CONVERSATION SUMMARY (context only, NOT a source):
{conversation_summary}

QUESTION:
{question}

PASSAGES:
{passages}
