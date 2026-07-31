---
name: chunk-contextualizer
version: 1
stage: ingestion
description: Situates a single chunk within its document so it retrieves on its own.
variables: [document_summary, heading_path, chunk_text]
---
You are given a document summary, the section a passage sits under, and the passage itself.

Write ONE short sentence that situates the passage within the document, so that a reader
encountering the passage alone would know what it refers to.

Rules:
- State the document and section context explicitly. Resolve pronouns and bare references.
- Do NOT summarise the passage. Do NOT add facts that are not present.
- Do NOT begin with "This passage" or "This chunk".
- Output the sentence only, with no preamble.

DOCUMENT SUMMARY:
{document_summary}

SECTION:
{heading_path}

PASSAGE:
{chunk_text}
