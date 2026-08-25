# Privacy and observability requirements

The business must be able to correlate one request across gateway entry, agent decision, retrieval outcome, Bedrock interaction, validation result, clinician review, and final business outcome.

Observability records must retain no prompts, model responses, retrieval queries or retrieved text, client or patient identifiers, credentials or authorization headers, or raw provider error strings. Only privacy-safe categorical outcome data may be retained.
