# Synthetic PII-redaction fixture

`applications.jsonl` is **synthetic test data**. Every value is fictional:

- names are placeholders ("Test Applicant One", "Test Entity One LLC");
- SSNs use area number `900`, which the SSA never issues;
- card numbers are published network test PANs (Visa, Mastercard, Amex,
  Discover) -- Luhn-valid, never real accounts;
- addresses use a placeholder street, town and the invalid state code `ZZ`.

It stands in for the raw application export ("KB dump") handed over during the
training engagement. Its only job is to be a negative fixture: it carries `ssn`
and `pan` fields so the loan assistant's offline PII gate can prove that a raw
application export is unsafe to embed (`services/loan-assistant/app/rag_eval.py`
`check_kb_dump_pii`, tested in
`services/loan-assistant/tests/test_rag_eval.py`). See
`adr/0005-rag-corpus-hygiene.md`.

The format is plain JSONL with one record per line, so this note lives beside the
file rather than in it. Nothing reads this file at runtime and it is never part
of the policy corpus.
