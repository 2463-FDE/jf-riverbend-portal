# Access-control matrix (exact current Riverbend roles)

Roles not listed below are not recognized and receive no access.

| Role | What shipped YAML actually grants | Retrieval this package allows |
|---|---|---|
| `patient` | `own_record.read`, `messages.*` | Patient-facing education and shared org education policies. Never clinician-only policies, never another patient. |
| `clinician` | `patients.read`, `records.*`, `appointments.read`, `consents.read`, `summary_review.decide`, `messages.*` | Patient education + clinician evidence + clinical org policy. |
| `nursing_ma` | Same permission set as `clinician` this cycle (deliberate) | Same retrieval classes as `clinician`. |
| `staff` | Deprecated legacy; still has broad patient-data permissions including `records.read` | Same clinical education as `clinician` for compatibility with existing accounts. Do not assign to new accounts. |
| `front_desk` | Registration/insurance/consent/scheduling; **no** `records.read` | `POL-FRONT-DESK-NO-CHART`, scheduling policy only. No A1c/clinical interpretation. |
| `lab` | `patients.read` + `records.write` **without** `records.read` | Lab posting policy only. No chart interpretation corpus. |
| `billing` | Coverage/payment; **no** clinical notes | Billing policy only. |
| `roi_clerk` | ROI + disclosures + audit; **no** clinical notes | ROI document-list policy only. |
| `scheduler` | Appointments only | Scheduling policy only. |
| `it_admin` | Accounts + audit; **no patient data** | IT/no-PHI policy only. |
| `management` | Oversight reporting; **no** demographics or notes | Oversight policy only. |

Access must follow both the role grant and the category rules below.

- `a1c_education` allowed: clinician, nursing_ma, patient, staff
- `cbc_anemia` allowed: clinician, nursing_ma, patient, staff
- `clinician_evidence` allowed: clinician, nursing_ma, staff
- `diabetes_self_management` allowed: clinician, nursing_ma, patient, staff
- `hypertension` allowed: clinician, nursing_ma, patient, staff
- `hypoglycemia_hyperglycemia` allowed: clinician, nursing_ma, patient, staff
- `kidney` allowed: clinician, nursing_ma, patient, staff
- `lipids_cv` allowed: clinician, nursing_ma, patient, staff
- `liver_masld` allowed: clinician, nursing_ma, patient, staff
- `medication_reconciliation` allowed: clinician, nursing_ma, patient, staff
- `medications_safety` allowed: clinician, nursing_ma, patient, staff
- `nutrition_lifestyle` allowed: clinician, nursing_ma, patient, staff
- `org_policy_workflow` allowed: billing, clinician, front_desk, it_admin, lab, management, nursing_ma, patient, roi_clerk, scheduler, staff
- `preventive_vaccines` allowed: clinician, nursing_ma, patient, staff
- `unapproved_injection_tests` allowed: (none)
- `urgent_warning_signs` allowed: clinician, nursing_ma, patient, staff
