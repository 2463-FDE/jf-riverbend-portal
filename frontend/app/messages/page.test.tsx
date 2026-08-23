import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Backend authorization already scopes /api/threads to the caller's own
// grants — what this page must get right is which ACTIONS it shows: compose
// is patient-only, close/reopen is staff-only, and neither role can reach a
// thread the mocked backend refuses.

vi.mock("../lib/session", () => ({
  apiFetch: vi.fn(),
  getUser: vi.fn(),
}));

import MessagesPage from "./page";
import { apiFetch, getUser } from "../lib/session";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}
function denied(status: number): Response {
  return { ok: false, status, json: async () => ({}) } as Response;
}

const THREAD_SUMMARY = {
  id: 1,
  patient_id: 1737,
  patient_name: "Priya Khan",
  subject: "Question about my results",
  status: "open",
  last_sender_name: "Dr. Grace Kim",
  last_message_at: "2026-08-23T00:00:00Z",
  unread_count: 1,
};

const THREAD_DETAIL = {
  id: 1,
  patient_id: 1737,
  patient_name: "Priya Khan",
  subject: "Question about my results",
  status: "open",
  created_at: "2026-08-23T00:00:00Z",
  messages: [
    { id: 1, thread_id: 1, sender_user_id: 901, sender_name: "Priya Khan", body: "Hi, quick question.", created_at: "2026-08-23T00:00:00Z" },
  ],
};

function mockRoutes(overrides: Partial<{ list: unknown; detail: unknown; reply: unknown }> = {}) {
  vi.mocked(apiFetch).mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "/api/threads" && (!init || !init.method)) {
      return ok(overrides.list ?? { items: [THREAD_SUMMARY] });
    }
    if (/\/api\/threads\/\d+$/.test(url)) {
      return ok(overrides.detail ?? THREAD_DETAIL);
    }
    if (/\/api\/threads\/\d+\/messages$/.test(url)) {
      return ok(
        overrides.reply ?? {
          id: 2, thread_id: 1, sender_user_id: 900, sender_name: "Dr. Grace Kim",
          body: "Reply text", created_at: "2026-08-23T00:05:00Z",
        }
      );
    }
    if (/\/api\/threads\/\d+\/status$/.test(url)) {
      return ok({ ...THREAD_DETAIL, status: "closed" });
    }
    if (url === "/api/patient/threads") {
      return ok({ ...THREAD_DETAIL, id: 2 });
    }
    return ok({});
  });
}

describe("Messages — patient view", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getUser).mockReturnValue({ username: "patient-1737", full_name: "Priya Khan", role: "patient" });
  });

  it("shows the inbox, a compose control, and no close/reopen control", async () => {
    mockRoutes();
    render(<MessagesPage />);

    expect(await screen.findByText("Question about my results")).toBeInTheDocument();
    expect(screen.getByText(/1 new/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new message to your care team/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /close thread|reopen thread/i })).not.toBeInTheDocument();
  });

  it("opens a thread and shows its messages", async () => {
    mockRoutes();
    render(<MessagesPage />);

    fireEvent.click(await screen.findByText("Question about my results"));

    expect(await screen.findByText("Hi, quick question.")).toBeInTheDocument();
    // A patient's own inbox does not repeat their own name as a row label.
    expect(screen.queryByText(/priya khan · /i)).not.toBeInTheDocument();
  });

  it("sends a reply and appends it to the thread", async () => {
    mockRoutes();
    render(<MessagesPage />);
    fireEvent.click(await screen.findByText("Question about my results"));
    await screen.findByText("Hi, quick question.");

    fireEvent.change(screen.getByPlaceholderText(/write a reply/i), { target: { value: "Thanks!" } });
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));

    await waitFor(() =>
      expect(
        vi.mocked(apiFetch).mock.calls.some(([url, init]) => /\/messages$/.test(url as string) && (init as RequestInit)?.method === "POST")
      ).toBe(true)
    );
  });

  it("starts a new thread through the compose form", async () => {
    mockRoutes();
    render(<MessagesPage />);
    fireEvent.click(await screen.findByRole("button", { name: /new message to your care team/i }));

    fireEvent.change(screen.getByLabelText(/subject/i), { target: { value: "New question" } });
    fireEvent.change(screen.getByLabelText(/message/i), { target: { value: "Body text" } });
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));

    await waitFor(() =>
      expect(vi.mocked(apiFetch).mock.calls.some(([url]) => url === "/api/patient/threads")).toBe(true)
    );
  });

  it("shows an empty state with no threads", async () => {
    mockRoutes({ list: { items: [] } });
    render(<MessagesPage />);

    expect(await screen.findByText(/no messages yet/i)).toBeInTheDocument();
  });

  it("shows a plain-language error when the inbox fails to load", async () => {
    vi.mocked(apiFetch).mockResolvedValue(denied(500));
    render(<MessagesPage />);

    expect(await screen.findByText(/could not load your messages/i)).toBeInTheDocument();
  });

  it("disables replying once a thread is closed", async () => {
    mockRoutes({ detail: { ...THREAD_DETAIL, status: "closed" } });
    render(<MessagesPage />);
    fireEvent.click(await screen.findByText("Question about my results"));

    await screen.findByText(/this thread is closed/i);
    expect(screen.getByPlaceholderText(/write a reply/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /^send$/i })).toBeDisabled();
  });
});

describe("Messages — clinician view", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getUser).mockReturnValue({ username: "drkim", full_name: "Dr. Grace Kim", role: "clinician" });
  });

  it("shows the patient name on each row and no compose control", async () => {
    mockRoutes();
    render(<MessagesPage />);

    expect(await screen.findByText("Priya Khan")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /new message to your care team/i })).not.toBeInTheDocument();
  });

  it("can close and reopen a thread", async () => {
    mockRoutes();
    render(<MessagesPage />);
    fireEvent.click(await screen.findByText("Question about my results"));
    await screen.findByText("Hi, quick question.");

    fireEvent.click(screen.getByRole("button", { name: /close thread/i }));

    await waitFor(() =>
      expect(
        vi.mocked(apiFetch).mock.calls.some(([url, init]) => /\/status$/.test(url as string) && (init as RequestInit)?.method === "POST")
      ).toBe(true)
    );
  });

  it("shows a denied thread as unavailable rather than exposing why", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url === "/api/threads") return ok({ items: [THREAD_SUMMARY] });
      if (/\/api\/threads\/\d+$/.test(url)) return denied(404);
      return ok({});
    });
    render(<MessagesPage />);
    fireEvent.click(await screen.findByText("Question about my results"));

    expect(await screen.findByText(/not available/i)).toBeInTheDocument();
    expect(screen.queryByText("Hi, quick question.")).not.toBeInTheDocument();
  });
});
