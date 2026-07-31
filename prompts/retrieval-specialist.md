---
name: retrieval-specialist
version: 1
stage: serving
description: Judges sufficiency and reformulates a weak query.
variables: [sub_question, passages, attempt, max_attempts]
---
Decide whether the retrieved passages answer the sub-question, and if not, produce a
better search query.

Return JSON with exactly these keys:
  sufficient     - true or false
  reasoning      - one sentence
  new_query      - a reformulated search query, or null when sufficient is true

Reformulation strategies, in order of preference:
  1. Use the corpus's own vocabulary instead of the user's paraphrase
  2. Narrow to the specific clause, fee, or condition being asked about
  3. Broaden to the parent topic when the specific term returns nothing
  4. Split a compound question into its most load-bearing half

Rules:
- "sufficient" means the passages contain the answer, not that they are on topic.
- Do NOT reformulate by adding synonyms alone; that rarely changes the results.
- This is attempt {attempt} of {max_attempts}. On the final attempt, prefer
  sufficient=true if the passages are usable at all.
- Output JSON only.

SUB-QUESTION:
{sub_question}

RETRIEVED PASSAGES:
{passages}
