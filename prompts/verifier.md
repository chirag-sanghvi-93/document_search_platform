---
name: verifier
version: 1
stage: serving
description: Grounds every claim in the evidence; retracts what is unsupported.
variables: [draft, passages]
---
Check the draft answer against the evidence, and return a corrected version.

For each claim in the draft:
  - Supported by the cited passage        -> keep it unchanged
  - Supported, but by a different passage -> correct the citation number
  - Not supported by any passage          -> REMOVE the claim
  - Partly supported                      -> hedge it to what the evidence shows

Return JSON with exactly these keys:
  verified_answer   - the corrected answer text, citation markers intact
  claims_retracted  - integer count of claims removed
  claims_hedged     - integer count of claims weakened

Rules:
- The passages are the ONLY admissible evidence. You have no other knowledge.
- Do not improve the wording of claims that are adequately supported.
- If everything is unsupported, return an answer stating the documents do not
  cover the question.
- Output JSON only.

EVIDENCE:
{passages}

DRAFT:
{draft}
