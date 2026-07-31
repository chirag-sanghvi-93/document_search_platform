---
name: document-summarizer
version: 1
stage: ingestion
description: One summary per document, reused by every chunk-level call.
variables: [title, heading_tree, first_pages]
---
Describe what this document is and what it covers, in 2-3 sentences.

Rules:
- Name the subject matter concretely. Avoid "this document discusses".
- State who or what it applies to, if the material says so.
- Do NOT invent scope that is not evidenced below.
- Output the sentences only.

TITLE:
{title}

SECTION HEADINGS:
{heading_tree}

OPENING PAGES:
{first_pages}
