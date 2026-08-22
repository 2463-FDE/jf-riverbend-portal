"use client";

import { useState } from "react";
import Link from "next/link";
import Card from "../components/Card";
import EligibilityStatus from "../components/EligibilityStatus";
import StateCombobox from "../components/StateCombobox";
import { IconEye, IconEyeOff } from "../components/icons";
import { apiFetch } from "../lib/session";
import type { IntakeInstructionsResponse, IntakeInstructionsStep, IntakeResponse } from "../lib/types";

interface Demographics {
  first_name: string;
  last_name: string;
  dob: string;
  gender: string;
  ssn: string;
  phone: string;
  email: string;
  address: string;
  city: string;
  state: string;
  zip_code: string;
}
interface Insurance {
  carrier: string;
  member_id: string;
  group_number: string;
  plan_type: string;
  policy_holder: string;
}
interface Consents {
  treatment: boolean;
  privacy: boolean;
  financial: boolean;
  communications: boolean;
}

const STEPS = ["Demographics & Contact", "Insurance", "Consents", "Review & Submit"];
// Stage 1 (feature-readiness): keys sent to POST /api/intake/instructions,
// in the same order as STEPS above.
const INSTRUCTIONS_STEPS: IntakeInstructionsStep[] = ["demographics", "insurance", "consents", "review"];

export default function IntakePage() {
  const [step, setStep] = useState(0);
  const [demo, setDemo] = useState<Demographics>({
    first_name: "",
    last_name: "",
    dob: "",
    gender: "",
    ssn: "",
    phone: "",
    email: "",
    address: "",
    city: "",
    state: "",
    zip_code: "",
  });
  const [ins, setIns] = useState<Insurance>({
    carrier: "",
    member_id: "",
    group_number: "",
    plan_type: "",
    policy_holder: "",
  });
  const [consents, setConsents] = useState<Consents>({
    treatment: false,
    privacy: false,
    financial: false,
    communications: false,
  });

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [eligibilityJobId, setEligibilityJobId] = useState<string | null>(null);

  const consentsOk = consents.treatment && consents.privacy;
  const demoOk = demo.first_name && demo.last_name && demo.dob;

  function next() {
    setStep((s) => Math.min(s + 1, STEPS.length - 1));
  }
  function back() {
    setStep((s) => Math.max(s - 1, 0));
  }

  async function submit() {
    setBusy(true);
    setResult(null);
    // Map the wizard's UI state to the intake-service contract
    // (services/intake-service/schemas.py):
    //  * consents — the backend expects a list of signed-consent kinds
    //    (list[str], persisted one row each), not the checkbox object. Send
    //    only the boxes the patient actually checked. Sending the object was
    //    a hard 422 ("Input should be a valid list"), so intake never
    //    submitted end-to-end.
    //  * insurance — the backend field is `payer_name`; the UI labels it
    //    "carrier". Only send an insurance block when there is something to
    //    send, so a patient with no coverage doesn't create an empty
    //    coverage row / eligibility job. (policy_holder has no backend
    //    column yet — dropped here; tracked as Stage 4 debt.)
    const consentKinds: string[] = [];
    if (consents.treatment) consentKinds.push("treatment_consent");
    if (consents.privacy) consentKinds.push("npp_ack");
    if (consents.financial) consentKinds.push("financial_agreement");
    if (consents.communications) consentKinds.push("communications_consent");
    const hasInsurance = Boolean(ins.carrier.trim() || ins.member_id.trim());
    const payload = {
      demographics: demo,
      insurance: hasInsurance
        ? {
            payer_name: ins.carrier,
            member_id: ins.member_id,
            group_number: ins.group_number,
            plan_type: ins.plan_type,
          }
        : null,
      consents: consentKinds,
    };
    try {
      const res = await apiFetch("/api/intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data: IntakeResponse & {
        error?: string;
        detail?: { error?: string; confidence?: string };
      } = await res.json();
      if (!res.ok || data?.error) {
        // A blocked exact-match duplicate (services/intake-service/app.py,
        // adr/0004/RIV-160) comes back as a 409 with detail.error set —
        // no patient/coverage/consent rows were created, unlike a generic
        // failure, so this gets its own message rather than "Submission failed."
        const isDuplicate = res.status === 409 && data?.detail?.error === "possible_duplicate_patient";
        setResult({
          ok: false,
          text: isDuplicate
            ? "We found an existing record that may match this patient. Please see the front desk to continue."
            : data?.error || "Submission failed.",
        });
      } else {
        setEligibilityJobId(data.eligibility_job_id ?? null);
        // The name is the form's OWN just-submitted values, not a round trip —
        // it is what was authoritatively written to patients.name a moment ago
        // (intake-service composes `name` from first_name/last_name the same
        // way), so there is nothing to look up. Matches the "{Name} — Patient
        // ID {id}" format required everywhere a patient is identified.
        const submittedName = [demo.first_name, demo.last_name].filter(Boolean).join(" ");
        setResult({
          ok: true,
          text: data.patient_id
            ? `Intake submitted. ${submittedName} — Patient ID ${data.patient_id}.`
            : "Intake submitted successfully.",
        });
      }
    } catch {
      setResult({ ok: false, text: "Could not reach the portal. Please try again." });
    } finally {
      setBusy(false);
    }
  }

  if (result?.ok) {
    return (
      <div className="rb-stack">
        <div className="rb-page-head">
          <h1>New Patient Intake</h1>
        </div>
        <Card>
          <div className="rb-alert rb-alert--ok" role="status" style={{ marginBottom: 16 }}>
            {result.text}
          </div>
          {eligibilityJobId && <EligibilityStatus jobId={eligibilityJobId} />}
          <p className="rb-muted">
            Thank you. Riverbend front-desk staff will review your intake before your first visit.
          </p>
          <Link className="rb-btn rb-btn--primary" href="/">
            Back to dashboard
          </Link>
        </Card>
      </div>
    );
  }

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>New Patient Intake</h1>
        <p>Complete the four steps below. It only takes a few minutes.</p>
      </div>

      <ol className="rb-steps" aria-label={`Step ${step + 1} of ${STEPS.length}`}>
        {STEPS.map((label, i) => {
          const state = i === step ? "active" : i < step ? "done" : "todo";
          return (
            <li
              key={label}
              className={`rb-steps__item${state !== "todo" ? ` rb-steps__item--${state}` : ""}`}
              aria-current={state === "active" ? "step" : undefined}
            >
              <span className="rb-steps__num">{state === "done" ? "✓" : i + 1}</span>
              <span className="rb-steps__label">{label}</span>
            </li>
          );
        })}
      </ol>

      {result && !result.ok && (
        <div className="rb-alert rb-alert--err" role="alert">
          {result.text}
        </div>
      )}

      <Card title={STEPS[step]}>
        {/* Codex review (2026-08-08, PR #24): key={step} forces React to
            unmount/remount IntakeInstructions on every step change, resetting
            its phase/summary state — without this, a patient who loaded the
            demographics summary and clicked "Continue" kept seeing that same
            text under the Insurance card. */}
        <IntakeInstructions key={INSTRUCTIONS_STEPS[step]} step={INSTRUCTIONS_STEPS[step]} />

        {step === 0 && (
          <div>
            <fieldset className="rb-subsection" style={{ border: "none", margin: 0, padding: 0 }}>
              <legend className="rb-subsection__title">Demographics</legend>
              <div className="rb-field-row">
                <Field id="first_name" label="First name" required value={demo.first_name}
                  onChange={(v) => setDemo({ ...demo, first_name: v })} />
                <Field id="last_name" label="Last name" required value={demo.last_name}
                  onChange={(v) => setDemo({ ...demo, last_name: v })} />
              </div>
              <div className="rb-field-row">
                <Field id="dob" label="Date of birth" type="date" required value={demo.dob}
                  onChange={(v) => setDemo({ ...demo, dob: v })} />
                <SelectField id="gender" label="Gender" value={demo.gender}
                  onChange={(v) => setDemo({ ...demo, gender: v })}
                  options={["", "Female", "Male", "Non-binary", "Prefer not to say"]} />
              </div>
              <div className="rb-field-row">
                <SsnField id="ssn" value={demo.ssn} onChange={(v) => setDemo({ ...demo, ssn: v })}
                  // Codex review (2026-08-09, PR #24, high): this said "Used
                  // for insurance verification only" — false (grepped
                  // services/intake-service/app.py: SSN is never sent to
                  // eligibility-service, which uses member_id only; SSN's
                  // actual use is the duplicate-patient match-key lookup,
                  // see _normalize_ssn/_find_match_candidates) and directly
                  // contradicted the new intake-instructions assistant's
                  // demographics text on this same screen
                  // (libs/intake_instructions/composer.py). Kept scoped to
                  // what this field is used for at intake time — full
                  // plaintext-storage/staff-access data-lifecycle disclosure
                  // (adr/0002) belongs in the Notice of Privacy Practices
                  // consent step, not a form-field hint.
                  hint="Optional — used to check for an existing patient record, not for insurance." />
              </div>
            </fieldset>

            <fieldset className="rb-subsection" style={{ border: "none", margin: 0, padding: 0 }}>
              <legend className="rb-subsection__title">Contact information</legend>
              <div className="rb-field-row">
                <Field id="phone" label="Phone" type="tel" value={demo.phone}
                  onChange={(v) => setDemo({ ...demo, phone: v })} />
                <Field id="email" label="Email" type="email" value={demo.email}
                  onChange={(v) => setDemo({ ...demo, email: v })} />
              </div>
              <Field id="address" label="Address" value={demo.address}
                onChange={(v) => setDemo({ ...demo, address: v })} />
              <div className="rb-field-row--3">
                <Field id="zip_code" label="ZIP code" value={demo.zip_code}
                  onChange={(v) => setDemo({ ...demo, zip_code: v })} />
                <Field id="city" label="City" value={demo.city}
                  onChange={(v) => setDemo({ ...demo, city: v })} />
                <StateCombobox id="state" label="State" value={demo.state}
                  onChange={(v) => setDemo({ ...demo, state: v })} />
              </div>
            </fieldset>
          </div>
        )}

        {step === 1 && (
          <fieldset style={{ border: "none", margin: 0, padding: 0 }}>
            <legend className="rb-muted" style={{ marginBottom: 12 }}>
              Enter your primary insurance.
            </legend>
            <div className="rb-field-row">
              <Field id="carrier" label="Insurance carrier" value={ins.carrier}
                onChange={(v) => setIns({ ...ins, carrier: v })} />
              <Field id="member_id" label="Member / Insurance ID" value={ins.member_id}
                onChange={(v) => setIns({ ...ins, member_id: v })} />
            </div>
            <div className="rb-field-row">
              <Field id="group_number" label="Group number" value={ins.group_number}
                onChange={(v) => setIns({ ...ins, group_number: v })} />
              <SelectField id="plan_type" label="Plan type" value={ins.plan_type}
                onChange={(v) => setIns({ ...ins, plan_type: v })}
                options={["", "HMO", "PPO", "EPO", "POS", "Medicare", "Medicaid", "Self-pay"]} />
            </div>
            <Field id="policy_holder" label="Policy holder name"
              hint="Leave blank if you are the policy holder."
              value={ins.policy_holder} onChange={(v) => setIns({ ...ins, policy_holder: v })} />
          </fieldset>
        )}

        {step === 2 && (
          <fieldset style={{ border: "none", margin: 0, padding: 0 }}>
            <legend className="rb-muted" style={{ marginBottom: 12 }}>
              Please review and acknowledge the following. Items marked required must be accepted.
            </legend>
            <Consent id="c_treatment" required checked={consents.treatment}
              onChange={(v) => setConsents({ ...consents, treatment: v })}
              title="Consent to treatment"
              body="I consent to medical care and treatment provided by Riverbend Community Health." />
            <Consent id="c_privacy" required checked={consents.privacy}
              onChange={(v) => setConsents({ ...consents, privacy: v })}
              title="Notice of privacy practices (HIPAA)"
              body="I acknowledge receipt of the Notice of Privacy Practices describing how my health information may be used and disclosed." />
            <Consent id="c_financial" checked={consents.financial}
              onChange={(v) => setConsents({ ...consents, financial: v })}
              title="Financial responsibility"
              body="I understand I am financially responsible for charges not covered by my insurance." />
            <Consent id="c_comms" checked={consents.communications}
              onChange={(v) => setConsents({ ...consents, communications: v })}
              title="Electronic communications (optional)"
              body="I agree to receive appointment reminders and portal notifications by email or text." />
          </fieldset>
        )}

        {step === 3 && (
          <div>
            <p className="rb-muted">Please confirm your information before submitting.</p>
            <h3 style={{ marginTop: 18 }}>Demographics</h3>
            <ReviewBlock rows={[
              ["First name", demo.first_name || "—"],
              ["Last name", demo.last_name || "—"],
              ["Date of birth", demo.dob || "—"],
              ["Gender", demo.gender || "—"],
              ["SSN", demo.ssn ? `•••-••-${demo.ssn.slice(-4)}` : "—"],
            ]} />
            <h3 style={{ marginTop: 18 }}>Contact</h3>
            <ReviewBlock rows={[
              ["Phone", demo.phone || "—"],
              ["Email", demo.email || "—"],
              ["Address", demo.address || "—"],
              ["City", demo.city || "—"],
              ["State", demo.state || "—"],
              ["ZIP code", demo.zip_code || "—"],
            ]} />
            <h3 style={{ marginTop: 18 }}>Insurance</h3>
            <ReviewBlock rows={[
              ["Carrier", ins.carrier || "—"],
              ["Member ID", ins.member_id || "—"],
              ["Group number", ins.group_number || "—"],
              ["Plan type", ins.plan_type || "—"],
              ["Policy holder", ins.policy_holder || "Self"],
            ]} />
            <h3 style={{ marginTop: 18 }}>Consents</h3>
            <ReviewBlock rows={[
              ["Treatment", consents.treatment ? "Accepted" : "Not accepted"],
              ["Privacy (HIPAA)", consents.privacy ? "Accepted" : "Not accepted"],
              ["Financial responsibility", consents.financial ? "Accepted" : "Declined"],
              ["Electronic communications", consents.communications ? "Accepted" : "Declined"],
            ]} />
          </div>
        )}

        <div className="rb-wizard-actions">
          <button className="rb-btn" onClick={back} disabled={step === 0 || busy} type="button">
            Back
          </button>
          {step < STEPS.length - 1 ? (
            <button
              className="rb-btn rb-btn--primary"
              onClick={next}
              type="button"
              disabled={(step === 0 && !demoOk) || (step === 2 && !consentsOk)}
            >
              Continue
            </button>
          ) : (
            <button
              className="rb-btn rb-btn--primary"
              onClick={submit}
              type="button"
              disabled={busy || !consentsOk || !demoOk}
            >
              {busy ? (
                <><span className="rb-spinner" aria-hidden="true" /> Submitting… this can take a few seconds</>
              ) : (
                "Submit intake"
              )}
            </button>
          )}
        </div>
      </Card>
    </div>
  );
}

// Stage 1 (feature-readiness): "Get plain-language summary" control for one
// intake wizard step. Sends only `step` — never any demographics/insurance
// field — to POST /api/intake/instructions, and renders the returned text
// as plain text (never dangerouslySetInnerHTML) so nothing the provider
// returns can execute in the browser.
type InstructionsPhase = "idle" | "loading" | "success" | "unavailable";

function IntakeInstructions({ step }: { step: IntakeInstructionsStep }) {
  const [phase, setPhase] = useState<InstructionsPhase>("idle");
  const [summary, setSummary] = useState<string | null>(null);

  async function load() {
    setPhase("loading");
    try {
      const res = await apiFetch("/api/intake/instructions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ step }),
      });
      if (!res.ok) {
        setPhase("unavailable");
        return;
      }
      const data = (await res.json()) as IntakeInstructionsResponse;
      if (!data?.summary) {
        setPhase("unavailable");
        return;
      }
      setSummary(data.summary);
      setPhase("success");
    } catch {
      setPhase("unavailable");
    }
  }

  return (
    <div className="rb-subsection" style={{ marginBottom: 16 }}>
      {phase === "idle" && (
        <button type="button" className="rb-btn" onClick={load}>
          What do I need for this step?
        </button>
      )}
      {phase === "loading" && (
        <span className="rb-muted">
          <span className="rb-spinner" aria-hidden="true" /> Getting a plain-language summary…
        </span>
      )}
      {phase === "success" && summary && (
        <div className="rb-alert" role="status">
          {summary}
        </div>
      )}
      {phase === "unavailable" && (
        <span className="rb-muted">
          Couldn&apos;t load a summary right now — please ask front-desk staff if you have questions.{" "}
          <button type="button" className="rb-btn" onClick={load}>
            Try again
          </button>
        </span>
      )}
    </div>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  type = "text",
  required = false,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  required?: boolean;
  hint?: string;
}) {
  return (
    <div className="rb-field">
      <label className="rb-field__label" htmlFor={id}>
        {label}
        {required && <span className="rb-field__req" aria-hidden="true">*</span>}
      </label>
      <input
        id={id}
        className="rb-input"
        type={type}
        value={value}
        required={required}
        aria-required={required}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && <span className="rb-field__hint">{hint}</span>}
    </div>
  );
}

function SsnField({
  id,
  value,
  onChange,
  hint,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  hint?: string;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="rb-field">
      <label className="rb-field__label" htmlFor={id}>
        SSN
      </label>
      <div className="rb-field__input-wrap">
        <input
          id={id}
          className="rb-input"
          type={visible ? "text" : "password"}
          inputMode="numeric"
          autoComplete="off"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        <button
          type="button"
          className="rb-field__reveal"
          aria-label={visible ? "Hide SSN" : "Show SSN"}
          onClick={() => setVisible((v) => !v)}
        >
          {visible ? <IconEyeOff /> : <IconEye />}
        </button>
      </div>
      {hint && <span className="rb-field__hint">{hint}</span>}
    </div>
  );
}

function SelectField({
  id,
  label,
  value,
  onChange,
  options,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <div className="rb-field">
      <label className="rb-field__label" htmlFor={id}>
        {label}
      </label>
      <select id={id} className="rb-select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o}>
            {o || "Select…"}
          </option>
        ))}
      </select>
    </div>
  );
}

function Consent({
  id,
  title,
  body,
  checked,
  onChange,
  required = false,
}: {
  id: string;
  title: string;
  body: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  required?: boolean;
}) {
  return (
    <div className="rb-checkbox">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        aria-required={required}
        onChange={(e) => onChange(e.target.checked)}
      />
      <label className="rb-checkbox__body" htmlFor={id}>
        <strong>
          {title}
          {required && <span className="rb-field__req" aria-hidden="true"> *</span>}
        </strong>
        {body}
      </label>
    </div>
  );
}

function ReviewBlock({ rows }: { rows: [string, string][] }) {
  return (
    <div className="rb-review">
      {rows.map(([k, v]) => (
        <div className="rb-review__row" key={k}>
          <span className="rb-review__key">{k}</span>
          <span>{v}</span>
        </div>
      ))}
    </div>
  );
}
