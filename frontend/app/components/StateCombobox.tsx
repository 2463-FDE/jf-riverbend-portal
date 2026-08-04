"use client";

import { useEffect, useId, useRef, useState } from "react";

/* Accessible combobox for selecting a U.S. state by its two-letter USPS
 * code. No dependency — implements the ARIA 1.2 "combobox with listbox
 * popup, list autocomplete" pattern by hand:
 * https://www.w3.org/WAI/ARIA/apg/patterns/combobox/
 *
 * `value` / `onChange` carry the two-letter code only (e.g. "CA"). The
 * visible text is always the full state name so the 1/2/3-letter prefix
 * typing the intake screen requires narrows by something a patient would
 * actually type ("Cal" -> California), not by the opaque code.
 */

export const US_STATES: { code: string; name: string }[] = [
  { code: "AL", name: "Alabama" }, { code: "AK", name: "Alaska" },
  { code: "AZ", name: "Arizona" }, { code: "AR", name: "Arkansas" },
  { code: "CA", name: "California" }, { code: "CO", name: "Colorado" },
  { code: "CT", name: "Connecticut" }, { code: "DE", name: "Delaware" },
  { code: "DC", name: "District of Columbia" }, { code: "FL", name: "Florida" },
  { code: "GA", name: "Georgia" }, { code: "HI", name: "Hawaii" },
  { code: "ID", name: "Idaho" }, { code: "IL", name: "Illinois" },
  { code: "IN", name: "Indiana" }, { code: "IA", name: "Iowa" },
  { code: "KS", name: "Kansas" }, { code: "KY", name: "Kentucky" },
  { code: "LA", name: "Louisiana" }, { code: "ME", name: "Maine" },
  { code: "MD", name: "Maryland" }, { code: "MA", name: "Massachusetts" },
  { code: "MI", name: "Michigan" }, { code: "MN", name: "Minnesota" },
  { code: "MS", name: "Mississippi" }, { code: "MO", name: "Missouri" },
  { code: "MT", name: "Montana" }, { code: "NE", name: "Nebraska" },
  { code: "NV", name: "Nevada" }, { code: "NH", name: "New Hampshire" },
  { code: "NJ", name: "New Jersey" }, { code: "NM", name: "New Mexico" },
  { code: "NY", name: "New York" }, { code: "NC", name: "North Carolina" },
  { code: "ND", name: "North Dakota" }, { code: "OH", name: "Ohio" },
  { code: "OK", name: "Oklahoma" }, { code: "OR", name: "Oregon" },
  { code: "PA", name: "Pennsylvania" }, { code: "RI", name: "Rhode Island" },
  { code: "SC", name: "South Carolina" }, { code: "SD", name: "South Dakota" },
  { code: "TN", name: "Tennessee" }, { code: "TX", name: "Texas" },
  { code: "UT", name: "Utah" }, { code: "VT", name: "Vermont" },
  { code: "VA", name: "Virginia" }, { code: "WA", name: "Washington" },
  { code: "WV", name: "West Virginia" }, { code: "WI", name: "Wisconsin" },
  { code: "WY", name: "Wyoming" },
];

const byCode = new Map(US_STATES.map((s) => [s.code, s]));

export default function StateCombobox({
  id,
  label,
  value,
  onChange,
  required = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (code: string) => void;
  required?: boolean;
}) {
  const listboxId = `${id}-listbox`;
  const reactId = useId();
  const optionId = (i: number) => `${id}-option-${reactId}-${i}`;

  const [inputValue, setInputValue] = useState(byCode.get(value)?.name ?? "");
  const [open, setOpen] = useState(false);
  const [highlighted, setHighlighted] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  // Keep displayed text in sync if the value changes from outside (e.g. form reset).
  useEffect(() => {
    setInputValue(byCode.get(value)?.name ?? "");
  }, [value]);

  const query = inputValue.trim().toLowerCase();
  const filtered = query
    ? US_STATES.filter((s) => s.name.toLowerCase().startsWith(query))
    : US_STATES;

  function commit(code: string) {
    const state = byCode.get(code);
    onChange(code);
    setInputValue(state?.name ?? "");
    setOpen(false);
  }

  function handleBlur() {
    // Give a mousedown-selected option a chance to commit first.
    window.setTimeout(() => {
      const typed = inputValue.trim();
      if (!typed) {
        onChange("");
        setInputValue("");
        setOpen(false);
        return;
      }
      const exact = US_STATES.find((s) => s.name.toLowerCase() === typed.toLowerCase());
      if (exact) {
        commit(exact.code);
      } else {
        // No confirmed match — revert to the last committed selection rather
        // than storing free text as a "state".
        setInputValue(byCode.get(value)?.name ?? "");
      }
      setOpen(false);
    }, 0);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setHighlighted((h) => Math.min(h + 1, Math.max(filtered.length - 1, 0)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      setHighlighted((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      if (open && filtered[highlighted]) {
        e.preventDefault();
        commit(filtered[highlighted].code);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
        setInputValue(byCode.get(value)?.name ?? "");
      }
    }
  }

  return (
    <div className="rb-field">
      <label className="rb-field__label" htmlFor={id}>
        {label}
        {required && <span className="rb-field__req" aria-hidden="true">*</span>}
      </label>
      <div className="rb-combobox">
        <input
          ref={inputRef}
          id={id}
          className="rb-input"
          type="text"
          role="combobox"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={open && filtered[highlighted] ? optionId(highlighted) : undefined}
          aria-required={required}
          autoComplete="off"
          value={inputValue}
          onChange={(e) => {
            setInputValue(e.target.value);
            setOpen(true);
            setHighlighted(0);
          }}
          onFocus={() => setOpen(true)}
          onBlur={handleBlur}
          onKeyDown={handleKeyDown}
        />
        {open && (
          <div className="rb-combobox__pop">
            {filtered.length > 0 ? (
              <ul className="rb-combobox__list" role="listbox" id={listboxId} ref={listRef}>
                {filtered.map((s, i) => (
                  <li
                    key={s.code}
                    id={optionId(i)}
                    role="option"
                    aria-selected={i === highlighted}
                    className={`rb-combobox__opt${i === highlighted ? " rb-combobox__opt--active" : ""}`}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      commit(s.code);
                    }}
                    onMouseEnter={() => setHighlighted(i)}
                  >
                    {s.name} <span className="rb-muted">({s.code})</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="rb-combobox__empty" role="status">
                No matching state
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
