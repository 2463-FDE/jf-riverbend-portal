"use client";

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  IconDashboard,
  IconCalendar,
  IconRecords,
  IconIntake,
  IconRoi,
  IconMessages,
  IconBilling,
  IconBell,
  IconLab,
  IconStethoscope,
} from "./icons";
import { clearSession, getUser, getToken, apiFetch } from "../lib/session";
import type { PortalUser } from "../lib/types";

interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
  soon?: boolean;
}

const NAV: NavItem[] = [
  { href: "/", label: "Dashboard", icon: <IconDashboard className="rb-nav__icon" /> },
  { href: "/appointments", label: "Appointments", icon: <IconCalendar className="rb-nav__icon" /> },
  { href: "/records", label: "Records", icon: <IconRecords className="rb-nav__icon" /> },
  { href: "/intake", label: "Intake", icon: <IconIntake className="rb-nav__icon" /> },
  { href: "/roi", label: "Release of Information", icon: <IconRoi className="rb-nav__icon" /> },
];

// Nothing left to park here — Billing became real (Coverage & Eligibility,
// W9.3, see NAV_COVERAGE below) the same way Messages did in W9.2. Kept as
// an empty list rather than removed outright: the next placeholder this
// product needs has somewhere to go without re-deriving the "Soon" styling.
const NAV_SOON: NavItem[] = [];

const NAV_MESSAGES: NavItem = {
  href: "/messages",
  label: "Messages",
  icon: <IconMessages className="rb-nav__icon" />,
};

// Deliberately NOT labelled "Billing" — this repository has coverage and
// eligibility capabilities and nothing else (no claims, invoices, balances,
// or payments exist anywhere in the schema), and the page itself refuses to
// imply otherwise. See app/coverage/page.tsx's own docstring.
const NAV_COVERAGE: NavItem = {
  href: "/coverage",
  label: "Coverage & Eligibility",
  icon: <IconBilling className="rb-nav__icon" />,
};

// w-9-2-planner P3: no permission gate — any authenticated session may ask
// (services/gateway/app.py::proxy_ask_policy_navigator uses require_session,
// not require_permission). Shown to every role, patients included.
const NAV_POLICY: NavItem = {
  href: "/policy",
  label: "Policy navigator",
  icon: <IconRecords className="rb-nav__icon" />,
};

// What a patient sees. Every entry in NAV above is a staff route that a
// patient account is refused — the `patient` role holds no staff permission
// at all — so showing them that menu would be five links that each fail. The
// navigation has to reflect the principal, not just the branding. Appointments
// and Coverage/Profile join this list only once their own patient-self routes
// exist and are tested (W9.3) — an entry that leads nowhere real is worse
// than no entry.
const NAV_PATIENT: NavItem[] = [
  { href: "/", label: "Home", icon: <IconDashboard className="rb-nav__icon" /> },
  { href: "/my-results", label: "Your results", icon: <IconLab className="rb-nav__icon" /> },
  NAV_MESSAGES,
  NAV_POLICY,
];

// Shown only to roles that actually hold summary_review.decide. `staff` was
// in this list while the gate was records.write; it is not any more, and
// leaving it would have pointed every legacy account at a link that lands on
// a 403 — a worse experience than no link, and misleading about who may
// review.
//
// Same caveat as the patient nav: a courtesy, not a control. The gateway and
// records-service refuse the route regardless of what is drawn here.
const _MAY_REVIEW = new Set(["clinician", "nursing_ma"]);

// Same two roles hold messages.read/messages.write (config/roles.yaml,
// W9.2) — kept as its own set rather than reused from _MAY_REVIEW because
// the two happen to match today for an unrelated reason (both are the
// clinical-documentation roles) and a future role could hold one permission
// without the other.
const _MAY_MESSAGE = new Set(["clinician", "nursing_ma"]);

// Roles holding billing.read (config/roles.yaml): front_desk, billing,
// management, and the deprecated staff role. Not clinician/nursing_ma/lab/
// roi_clerk/scheduler/it_admin — none of those holds any billing.* permission.
const _MAY_VIEW_COVERAGE = new Set(["front_desk", "billing", "management", "staff"]);

const NAV_REVIEW: NavItem = {
  href: "/review-queue",
  label: "Review queue",
  icon: <IconStethoscope className="rb-nav__icon" />,
};

// Nothing here is an authorization boundary — that lives in the gateway and
// the records service. Hiding a link the caller cannot use is a courtesy to
// them, not a control; a patient typing /records still gets a 403 from the
// server, which is where it matters.
function navFor(user: PortalUser | null): NavItem[] {
  if (user?.role === "patient") return NAV_PATIENT;
  const role = user?.role ?? "";
  let items = _MAY_REVIEW.has(role) ? [...NAV, NAV_REVIEW] : [...NAV];
  if (_MAY_MESSAGE.has(role)) items = [...items, NAV_MESSAGES];
  if (_MAY_VIEW_COVERAGE.has(role)) items = [...items, NAV_COVERAGE];
  return [...items, NAV_POLICY];
}

function Logo({ className }: { className?: string }) {
  // Simple "river bend" mark — a teal rounded square with a flowing wave.
  return (
    <svg className={className} viewBox="0 0 40 40" aria-hidden="true" focusable="false">
      <rect width="40" height="40" rx="9" fill="#0f7c91" />
      <path
        d="M7 26c4 0 4-6 8-6s4 6 8 6 4-6 8-6"
        fill="none"
        stroke="#ffffff"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
      <path d="M20 9v8M16 13h8" stroke="#bfe7ee" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(href + "/");
}

export default function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<PortalUser | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Routes a person can reach with no account at all.
  //
  // /activate is public by necessity: the patient redeeming an invitation
  // code does not have an account yet — creating one is the entire point of
  // the page. Before this list existed the shell exempted only /login, so an
  // unauthenticated patient opening /activate was bounced straight to the
  // sign-in screen and the patient half of the flow was unreachable in the
  // running app. Every API-level test passed throughout, because none of them
  // went through a browser.
  //
  // /login/mfa and /mfa/enroll are the same case for a different reason
  // (w8-planner-2 MFA rollout): a forced first-time enrollment or a
  // login-challenge completion happens BEFORE any session exists — the
  // caller is holding only a short-lived, single-purpose challenge token
  // (lib/session.ts's PendingMfaChallenge), never a real one. Bouncing
  // either page to /login the same way a private route would defeat the
  // whole flow: there would be nowhere to finish it.
  //
  // This is not an authorization decision. Nothing sensitive is served on
  // these routes; /activate's own endpoint is public at the gateway and
  // returns one identical answer for every failure so it cannot be used to
  // discover valid codes.
  const isPublicRoute =
    pathname === "/login" ||
    pathname === "/activate" ||
    pathname === "/login/mfa" ||
    pathname === "/mfa/enroll";

  // Hydrate the signed-in user from sessionStorage (see lib/session.ts for
  // why not localStorage). There is no real route guard here beyond "no token
  // → bounce to /login"; the gateway is what actually enforces expiry, on
  // both an idle and an absolute TTL.
  useEffect(() => {
    if (isPublicRoute) return;
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setUser(getUser());
  }, [isPublicRoute, pathname, router]);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  async function signOut() {
    // Shared-workstation fix: this used to swallow any failure and clear
    // local storage regardless, which showed the user a signed-out screen
    // while their session stayed valid on the server — on a machine someone
    // else was about to use. Only clear locally once the gateway confirms
    // the session is actually gone.
    setSignOutError(null);
    setSigningOut(true);
    try {
      const res = await apiFetch("/api/logout", { method: "POST" });
      if (!res.ok) throw new Error(`logout failed (${res.status})`);
    } catch {
      setSigningOut(false);
      setSignOutError(
        "We could not end your session. You are still signed in — please try again, and tell IT if it keeps failing."
      );
      return;
    }
    clearSession();
    router.replace("/login");
  }

  // The login page renders its own full-bleed layout, no shell.
  if (isPublicRoute) return <>{children}</>;

  const nav = navFor(user);
  const pageTitle =
    nav.find((n) => isActive(pathname, n.href))?.label ?? "Patient Portal";

  return (
    <div className="rb-shell">
      <a href="#rb-main" className="rb-skip-link">
        Skip to main content
      </a>

      <aside className="rb-sidebar">
        <div className="rb-sidebar__brand">
          <Logo className="rb-sidebar__mark" />
          <div>
            <div className="rb-sidebar__name">Riverbend</div>
            <div className="rb-sidebar__tag">Community Health</div>
          </div>
        </div>

        <nav className="rb-nav" aria-label="Primary">
          {nav.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.label}
                href={item.href}
                className={`rb-nav__item${active ? " rb-nav__item--active" : ""}`}
                aria-current={active ? "page" : undefined}
              >
                {item.icon}
                <span>{item.label}</span>
              </Link>
            );
          })}

          {/* NAV_SOON is currently empty for every role (Billing became
              real in W9.3, the same way Messages did in W9.2) — the whole
              section hides rather than showing a "More" label over nothing,
              same reasoning as hiding it outright for patients below. */}
          {(user?.role === "patient" ? [] : NAV_SOON).length > 0 && (
            <>
              <div className="rb-nav__section">More</div>
              {NAV_SOON.map((item) => (
                <span
                  key={item.label}
                  className="rb-nav__item rb-nav__item--disabled"
                  aria-disabled="true"
                >
                  {item.icon}
                  <span>{item.label}</span>
                  <span className="rb-nav__soon">Soon</span>
                </span>
              ))}
            </>
          )}
        </nav>
      </aside>

      <header className="rb-topbar">
        <span className="rb-topbar__title">{pageTitle}</span>
        <span className="rb-topbar__spacer" />

        {/* The "1 new" dot below is hardcoded, not a real unread count — true
            for every session, but only patients are told so anywhere in the
            product. Suppressed for patient sessions until W9.2 messaging
            gives it a real source to report; staff behavior is unchanged
            (out of scope here, not evaluated). */}
        <button
          className="rb-iconbtn"
          aria-label={user?.role === "patient" ? "Notifications" : "Notifications (1 new)"}
          type="button"
        >
          <IconBell />
          {user?.role !== "patient" && <span className="rb-iconbtn__dot" aria-hidden="true" />}
        </button>

        <div className="rb-usermenu" ref={menuRef}>
          <button
            className="rb-usermenu__btn"
            onClick={() => setMenuOpen((o) => !o)}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            type="button"
          >
            <span className="rb-avatar" aria-hidden="true">
              {initials(user?.full_name ?? user?.username ?? "?")}
            </span>
            <span className="rb-usermenu__meta">
              <span className="rb-usermenu__name">
                {user?.full_name ?? user?.username ?? "Guest"}
              </span>
              {user?.role && <span className="rb-usermenu__role">{user.role}</span>}
            </span>
          </button>
          {menuOpen && (
            <div className="rb-usermenu__pop" role="menu">
              <div style={{ padding: "6px 10px" }} className="rb-muted">
                Signed in as<br />
                <strong style={{ color: "var(--rb-text)" }}>
                  {user?.username ?? "—"}
                </strong>
              </div>
              <div className="rb-usermenu__divider" />
              <button
                role="menuitem"
                type="button"
                onClick={signOut}
                disabled={signingOut}
              >
                {signingOut ? "Signing out…" : "Sign out"}
              </button>
              {signOutError && (
                <p
                  role="alert"
                  style={{
                    margin: "6px 10px 8px",
                    fontSize: "0.78rem",
                    lineHeight: 1.4,
                    color: "var(--rb-danger, #b3261e)",
                  }}
                >
                  {signOutError}
                </p>
              )}
            </div>
          )}
        </div>
      </header>

      <main className="rb-content" id="rb-main">
        {children}
      </main>
    </div>
  );
}
