---
name: planner
version: 4
stage: serving
description: Classifies, rewrites and decomposes in one structured call.
variables: [corpus_description, conversation_summary, recent_turns, question]
---
Plan how to answer the user's question against the described corpus.

Return JSON with exactly these keys:
  intent              - one of: lookup | comparison | summary | capability | out_of_scope
  standalone_question - the question rewritten to stand alone, with all references
                        to earlier turns resolved into explicit terms
  sub_questions       - 1 to 4 self-contained search questions

Rules:
- Resolve every pronoun and elision using the conversation context. "What about the
  other one?" must become an explicit question naming the subject.

- PREFER ONE SUB-QUESTION. Most questions need exactly one. Decompose only when
  the QUESTION ITSELF asks for more than one thing.

- ⚠️ BUT: if intent is "comparison", you MUST return ONE SUB-QUESTION PER THING
  BEING COMPARED, each naming only its own subject. "Whose baggage rules are
  better, A or B?" becomes:
      ["what are A's baggage rules?", "what are B's baggage rules?"]
  NOT a single sub-question repeating the whole comparison. A comparison
  searched as one query retrieves only whichever side scores higher, so the
  other side is never fetched and no comparison is possible.

- Do NOT invent a comparison the user did not ask for. The corpus may contain
  several documents, versions, or organisations; that is not a reason to compare
  them. "What is the baggage charge?" is ONE lookup, even if three documents
  mention baggage. Use "comparison" only when the user explicitly asks how two
  things differ, or which of two is better.

- Do NOT add qualifiers the user did not state. If they did not say "domestic",
  "international", or name a specific document, neither should your
  sub-questions. Added qualifiers narrow the search away from the general rule
  that usually answers the question.

- Keep sub-questions close to the user's own wording. You are splitting the
  question, not rewriting it into something more specific.

- Use "capability" when the user is asking about THIS ASSISTANT rather than about
  the documents: "what can I ask you?", "what do you know about?", "what
  documents do you have?", "help", "what can you do?". These are answered by
  describing the corpus, so they need no sub-questions — return an empty list.
  They are NOT out_of_scope: the user is not asking about an uncovered subject,
  they are asking what the subjects are.

- Use "out_of_scope" ONLY when the corpus description clearly does not cover the
  subject. When uncertain, prefer to retrieve: wrongly refusing a legitimate
  question is the worse error, because a wrong "in scope" merely costs a search
  that finds nothing and declines anyway.

- Never more than four sub-questions.
- Output JSON only.

WHAT THIS CORPUS COVERS:
{corpus_description}

CONVERSATION SUMMARY:
{conversation_summary}

RECENT TURNS:
{recent_turns}

QUESTION:
{question}
