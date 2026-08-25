# Conflict-resolution rule

When two approved sources disagree:

1. **Patient-specific clinician-approved record**, if the caller is authorized for that patient and the record is in-scope. That record still cannot authorize a new diagnosis or prescription in generated text.
2. **Current official / federal guidance** (CDC, NIH/NIDDK, MedlinePlus/NLM, FDA, AHRQ, HHS, CMS, USPSTF as applicable), preferring the current effective or update date.
3. **Professional guideline** (citation-only records in this package; do not invent full text).
4. **Systematic review / peer-reviewed open source** (none shipped as full text here).
5. **Patient education** derivatives and synthetic teaching examples.

Within a tier, prefer the current effective/version date. `POL-A1C-MONITOR-CURRENT` outranks `POL-A1C-MONITOR-STALE`.

**Never silently blend unresolved conflicts.** Cite both `citation_id` values, state that the sources disagree, refuse a definitive conclusion, and route to clinician review.

Separate education from patient-specific clinical decisions. Generated text may explain a public range (for example A1c diagnostic cut-points from federal education) and must not assign a personal target.
