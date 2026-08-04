import type { SVGProps } from "react";

/* Minimal inline stroke icons (no icon-library dependency). */

type IconProps = SVGProps<SVGSVGElement>;

function base(props: IconProps) {
  return {
    width: 20,
    height: 20,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    focusable: false,
    ...props,
  };
}

export const IconDashboard = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="3" width="7" height="9" rx="1" />
    <rect x="14" y="3" width="7" height="5" rx="1" />
    <rect x="14" y="12" width="7" height="9" rx="1" />
    <rect x="3" y="16" width="7" height="5" rx="1" />
  </svg>
);

export const IconCalendar = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="4" width="18" height="17" rx="2" />
    <path d="M16 2v4M8 2v4M3 10h18" />
  </svg>
);

export const IconRecords = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M6 2h9l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z" />
    <path d="M14 2v6h6M9 13h6M9 17h6M9 9h2" />
  </svg>
);

export const IconIntake = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M9 4h6a2 2 0 0 1 2 2v0H7v0a2 2 0 0 1 2-2Z" />
    <rect x="5" y="4" width="14" height="17" rx="2" />
    <path d="M9 11l2 2 4-4" />
  </svg>
);

export const IconRoi = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 4h16v12H7l-3 3V4Z" />
    <path d="M8 9h8M8 12h5" />
  </svg>
);

export const IconMessages = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M21 11.5a8.4 8.4 0 0 1-9 8 9 9 0 0 1-3.8-.8L3 20l1.3-3.9A8 8 0 0 1 3 11.5a8.4 8.4 0 0 1 9-8 8.4 8.4 0 0 1 9 8Z" />
  </svg>
);

export const IconBilling = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="2" y="5" width="20" height="14" rx="2" />
    <path d="M2 10h20M6 15h4" />
  </svg>
);

export const IconBell = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
    <path d="M13.7 21a2 2 0 0 1-3.4 0" />
  </svg>
);

export const IconPlus = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const IconSearch = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

export const IconLab = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4A2 2 0 0 0 19 18l-5-9V3" />
    <path d="M7.5 14h9" />
  </svg>
);

export const IconStethoscope = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M5 3v5a4 4 0 0 0 8 0V3" />
    <path d="M5 3H4M13 3h1M9 16v1a4 4 0 0 0 8 0v-1" />
    <circle cx="18" cy="14" r="2" />
  </svg>
);

export const IconClock = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);

export const IconPin = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" />
    <circle cx="12" cy="10" r="3" />
  </svg>
);

export const IconHeart = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 21s-7-4.6-9.5-9A5 5 0 0 1 12 6a5 5 0 0 1 9.5 6c-2.5 4.4-9.5 9-9.5 9Z" />
  </svg>
);

export const IconEye = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);

export const IconEyeOff = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2 12s3.6-7 10-7c1.6 0 3 .3 4.2.8M22 12s-1.4 2.7-4 4.7M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    <path d="M3 3l18 18" />
  </svg>
);
