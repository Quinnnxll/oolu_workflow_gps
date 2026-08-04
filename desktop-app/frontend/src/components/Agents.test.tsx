import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { AgentThread } from "./Agents";
import type { RosterAgent } from "../api";

// The device picker can't open a real dialog under jsdom — the mock
// hands the desk one picked photo, the way a member would.
vi.mock("../device", () => ({
  pickLocalFiles: vi.fn(async () => [
    new File(["x"], "pier.jpg", { type: "image/jpeg" }),
  ]),
  // The inline fallback saveToDrawer takes on a blob-less host.
  fileToDrawerContent: vi.fn(async () => ({
    content: "data:image/jpeg;base64,eA==",
    mediaType: "image/jpeg",
  })),
  saveToDevice: vi.fn(),
}));

// The press panel (A1): the shelf and the contribute form live at the
// head of the News thread; everything renders from the server's own
// words (taxonomy, license terms), and publishing walks the one door
// with consent carried explicitly.

let routes: Record<string, { status: number; body: unknown }>;
let calls: { method: string; path: string; body: unknown }[];

const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
  const u = new URL(String(input), "http://local.test");
  const method = init?.method ?? "GET";
  // A raw-bytes body (the blob door's File) is not JSON — keep it as-is.
  let body: unknown;
  if (init?.body) {
    try {
      body = JSON.parse(String(init.body));
    } catch {
      body = init.body;
    }
  }
  calls.push({ method, path: u.pathname, body });
  const hit = routes[`${method} ${u.pathname}`] ?? { status: 200, body: {} };
  return {
    ok: hit.status >= 200 && hit.status < 300,
    status: hit.status,
    text: async () => JSON.stringify(hit.body),
    json: async () => hit.body,
    blob: async () => new Blob([JSON.stringify(hit.body)]),
  } as Response;
});

beforeEach(() => {
  routes = {};
  calls = [];
  localStorage.clear();
  window.__OOLU_API__ = "http://local.test";
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockClear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OOLU_API__;
});

const NEWS: RosterAgent = {
  agent_id: "news",
  name: "News",
  tagline: "Stories from members",
  scope: "I collect member contributions.",
  ahead: "Stories arrive in phase A2.",
  seat: "news.compose",
};

const GENRES = {
  status: 200,
  body: {
    taxonomy_version: 1,
    items: [
      { key: "local", label: "Around me", description: "Nearby happenings." },
      { key: "food", label: "Food", description: "Cooking and eating." },
    ],
    licenses: [
      {
        key: "oolu-members-1",
        name: "OoLu members license v1",
        terms: "Visible to members; credited; revocable-forward.",
      },
    ],
  },
};

describe("The story reader (N0)", () => {
  it("expands the pushed story block into the reader and measures the read", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = { status: 200, body: { items: [] } };
    // The edition landed server-side as the News agent's own turn — the
    // story block PERSISTED with it, so a fresh device renders it.
    routes["GET /v1/chat/history"] = {
      status: 200,
      body: {
        items: [
          {
            seq: 1,
            kind: "assistant",
            body: "Good morning — your edition:",
            at: "t",
            agent: "news",
            block: {
              kind: "story",
              items: [
                {
                  story_id: "s1",
                  headline: "The pier queues again",
                  preview: "Forty stalls…",
                  genres: ["local"],
                  bylines: ["alice"],
                },
              ],
            },
          },
        ],
      },
    };
    routes["GET /v1/press/stories/s1"] = {
      status: 200,
      body: {
        story_id: "s1",
        headline: "The pier queues again",
        prose: "Forty stalls this morning.",
        genres: ["local"],
        lineage: [{ contribution_id: "c1", author: "alice", weight: 1 }],
        rubric_version: 1,
        source: "desk",
        created_at: "t",
        media: [],
      },
    };
    routes["GET /v1/press/stories/s1/metrics"] = {
      status: 200,
      body: { story_id: "s1", revealed: false, reason: "not enough readers yet" },
    };
    routes["GET /v1/profiles/alice"] = {
      status: 200,
      body: { username: "alice", display_name: "Alice", has_photo: false },
    };
    routes["POST /v1/press/stories/s1/feedback"] = {
      status: 200,
      body: { recorded: true },
    };
    render(<AgentThread agent={NEWS} />);

    // The block renders from the persisted history; the tap opens the
    // reader — the pane, with a way back.
    fireEvent.click(await screen.findByText("The pier queues again"));
    expect(
      await screen.findByText("Forty stalls this morning."),
    ).toBeTruthy();
    // Below the floor, the benchmark line is the honest reason.
    expect(await screen.findByText("not enough readers yet")).toBeTruthy();

    // The like tap counts (and echoes the verdict).
    fireEvent.click(screen.getByLabelText("Like"));
    expect(await screen.findByText("noted")).toBeTruthy();

    // Leaving sends ONE read receipt with the honest measurements: the
    // dwell, and completion (the whole story was visible).
    fireEvent.click(screen.getByText("← back"));
    const read = calls.find(
      (c) =>
        c.method === "POST" &&
        c.path === "/v1/press/stories/s1/feedback" &&
        (c.body as { signal?: string }).signal === "read",
    );
    expect(read).toBeTruthy();
    const receipt = read!.body as { dwell_ms: number; completed: boolean };
    expect(receipt.completed).toBe(true);
    expect(typeof receipt.dwell_ms).toBe("number");
    // And the thread is back, block still standing.
    expect(await screen.findByText(/your edition/)).toBeTruthy();
  });
});

describe("PressPanel (inside the News thread)", () => {
  it("renders the shelf with bylines and credits a retelling", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = {
      status: 200,
      body: {
        items: [
          {
            contribution_id: "c1",
            author: "bob",
            title: "The harbor market reopened",
            body: "Forty stalls this morning.",
            genres: ["local"],
            license: "oolu-members-1",
            media: [],
            similar_to: "c0",
            similarity: 0.7,
            taxonomy_version: 1,
            created_at: "2026-07-12T09:00:00Z",
            superseded_at: null,
          },
        ],
      },
    };
    routes["GET /v1/profiles/bob"] = {
      status: 200,
      body: { username: "bob", display_name: "Bob R.", has_photo: false },
    };
    render(<AgentThread agent={NEWS} />);

    expect(await screen.findByText("The harbor market reopened")).toBeTruthy();
    // The byline resolves the author's display name...
    expect(await screen.findByText("Bob R.")).toBeTruthy();
    // ...the genre chip speaks the taxonomy's label, not the key...
    expect(screen.getByText("Around me")).toBeTruthy();
    // ...and a flagged retelling credits the original, visibly.
    expect(screen.getByText("retells an earlier piece")).toBeTruthy();
    // Not my piece: no unpublish control.
    expect(screen.queryByText("Unpublish")).toBeNull();
  });

  it("has no manual compose form — the conversation is the door", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = {
      status: 200,
      body: { items: [] },
    };
    render(<AgentThread agent={NEWS} />);
    expect(await screen.findByText("Contributions")).toBeTruthy();
    // The desk detects material in the thread; nobody fills a form.
    expect(screen.queryByText("Write a piece")).toBeNull();
    expect(screen.queryByPlaceholderText("Title")).toBeNull();
  });

  it("sends raw material to the desk — words and attachments together", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = {
      status: 200,
      body: { items: [] },
    };
    routes["POST /v1/files/upload"] = {
      status: 201,
      body: {
        file_id: "f1",
        name: "pier.jpg",
        media_type: "image/jpeg",
        size: 1,
        created_at: "2026-07-12T09:00:00Z",
        updated_at: "2026-07-12T09:00:00Z",
      },
    };
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply:
          "Ready to publish: “The pier queues again” — filed under Around me.",
        source: "desk",
        actions: [],
        agent: "news",
      },
    };
    render(<AgentThread agent={NEWS} />);

    // 📎 uploads the photo and holds it as a removable chip.
    fireEvent.click(
      await screen.findByLabelText("Add photo, video, or audio"),
    );
    const chip = await screen.findByTitle("Remove attachment");
    expect(chip.textContent).toContain("pier.jpg");

    fireEvent.change(screen.getByPlaceholderText("Message News…"), {
      target: { value: "The pier queued again this morning." },
    });
    fireEvent.click(screen.getByLabelText("Send"));

    // The desk's review arrives as the thread's own reply...
    expect(await screen.findByText(/Ready to publish/)).toBeTruthy();
    // ...and the message carried the attachment refs for the desk.
    const turn = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/chat",
    );
    expect(turn?.body).toMatchObject({
      message: "The pier queued again this morning.",
      agent: "news",
      file_ids: ["f1"],
    });
    // The chip is spent with the send.
    expect(screen.queryByTitle("Remove attachment")).toBeNull();
  });

  it("offers unpublish only on your own pieces", async () => {
    localStorage.setItem("oolu_token", "tok");
    localStorage.setItem("oolu_principal", "alice");
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = {
      status: 200,
      body: {
        items: [
          {
            contribution_id: "c2",
            author: "alice",
            title: "My own piece",
            body: "Words of mine.",
            genres: ["food"],
            license: "oolu-members-1",
            media: [],
            similar_to: null,
            similarity: null,
            taxonomy_version: 1,
            created_at: "2026-07-12T09:00:00Z",
            superseded_at: null,
          },
        ],
      },
    };
    render(<AgentThread agent={NEWS} />);
    expect(await screen.findByText("My own piece")).toBeTruthy();
    fireEvent.click(screen.getByText("Unpublish"));
    const unpublish = calls.find(
      (c) =>
        c.method === "POST" &&
        c.path === "/v1/press/contributions/c2/unpublish",
    );
    expect(unpublish).toBeTruthy();
  });

  it("renders stories with lineage bylines, hidden scoring, emoji taps", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = {
      status: 200,
      body: {
        items: [
          {
            story_id: "st1",
            headline: "The pier queues again",
            prose: "Forty stalls returned this morning.",
            genres: ["local"],
            lineage: [
              { contribution_id: "c1", author: "alice", weight: 0.6 },
              { contribution_id: "c2", author: "bob", weight: 0.4 },
            ],
            rubric_version: 1,
            source: "desk",
            created_at: "2026-07-13T08:00:00Z",
          },
        ],
        personalized: false,
        edition_schedule: null,
      },
    };
    routes["POST /v1/press/stories/st1/feedback"] = {
      status: 200,
      body: { recorded: false, reason: "personalization is off" },
    };
    render(<AgentThread agent={NEWS} />);

    expect(await screen.findByText("The pier queues again")).toBeTruthy();
    // Both cited contributors' bylines render — the lineage speaks.
    expect(await screen.findByText("alice")).toBeTruthy();
    expect(await screen.findByText("bob")).toBeTruthy();
    // The scoring stays the house's own: no "why" panel renders.
    expect(screen.queryByText("Why this story")).toBeNull();
    // The taps speak emoji, the words stay for the screen reader — and
    // a tap answers honestly when personalization is off.
    fireEvent.click(screen.getByLabelText("Like"));
    expect(
      await screen.findByText("not recorded — personalization is off"),
    ).toBeTruthy();
    expect(screen.getByLabelText("Skip").textContent).toContain("⏭");
    const tap = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/press/stories/st1/feedback",
    );
    expect(tap?.body).toMatchObject({ signal: "like" });
  });

  it("renders a story's attached media through the press media door", async () => {
    const createURL = vi.fn(() => "blob:media-1");
    URL.createObjectURL = createURL as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = {
      status: 200,
      body: {
        items: [
          {
            story_id: "st1",
            headline: "The pier queues again",
            prose: "Forty stalls returned.",
            genres: ["local"],
            lineage: [{ contribution_id: "c1", author: "alice", weight: 1 }],
            rubric_version: 1,
            source: "desk",
            created_at: "2026-07-13T08:00:00Z",
            media: [
              {
                contribution_id: "c1",
                index: 0,
                media_type: "image/jpeg",
                name: "pier.jpg",
              },
            ],
          },
        ],
        personalized: false,
        edition_schedule: null,
      },
    };
    render(<AgentThread agent={NEWS} />);

    // The photo rides the lineage: fetched through the press media door
    // (the bearer token travels with it) and shown as an object URL.
    const img = await screen.findByAltText("pier.jpg");
    expect(img.getAttribute("src")).toBe("blob:media-1");
    const fetched = calls.find(
      (c) =>
        c.method === "GET" && c.path === "/v1/press/contributions/c1/media/0",
    );
    expect(fetched).toBeTruthy();
  });

  it("cranks the newsroom and toggles the morning edition", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = {
      status: 200,
      body: { items: [], personalized: false, edition_schedule: null },
    };
    routes["POST /v1/press/newsroom/run"] = {
      status: 200,
      body: { composed: 1, items: [] },
    };
    routes["POST /v1/press/edition/schedule"] = {
      status: 200,
      body: { edition_schedule: { schedule_id: "sch1", at_minute: 480 } },
    };
    render(<AgentThread agent={NEWS} />);

    fireEvent.click(await screen.findByText("Compose now"));
    expect(
      calls.some(
        (c) => c.method === "POST" && c.path === "/v1/press/newsroom/run",
      ),
    ).toBeTruthy();

    fireEvent.click(screen.getByText("Morning edition"));
    expect(await screen.findByText("Morning edition ✓")).toBeTruthy();
    const scheduled = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/press/edition/schedule",
    );
    expect(scheduled?.body).toMatchObject({ enabled: true });
  });

  it("picks the stream through the genre chips block — the tap speaks", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = { status: 200, body: { items: [] } };
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply: "The streams members publish into.",
        source: "desk",
        agent: "news",
        block: { kind: "genres", items: GENRES.body.items },
      },
    };
    render(<AgentThread agent={NEWS} />);
    fireEvent.change(screen.getByPlaceholderText("Message News…"), {
      target: { value: "genres" },
    });
    fireEvent.click(screen.getByLabelText("Send"));
    fireEvent.click(await screen.findByRole("button", { name: "Food" }));
    // The tap SPEAKS: the label goes back through the conversation.
    const spoken = calls.filter(
      (c) => c.method === "POST" && c.path === "/v1/chat",
    );
    expect(spoken.length).toBe(2);
    expect(spoken[1]?.body).toMatchObject({ message: "Food", agent: "news" });
  });

  it("holds the ad slot behind the versioned consent, then labels it", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = {
      status: 200,
      body: {
        items: [
          {
            story_id: "st1",
            headline: "The pier queues again",
            prose: "Forty stalls returned.",
            genres: ["local"],
            lineage: [{ contribution_id: "c1", author: "alice", weight: 1 }],
            rubric_version: 1,
            source: "desk",
            created_at: "2026-07-15T08:00:00Z",
          },
        ],
        personalized: false,
        edition_schedule: null,
      },
    };
    // Before acceptance: the slot renders the consent card, no ad.
    routes["GET /v1/press/ads"] = {
      status: 200,
      body: { placement: null, reason: "consent" },
    };
    routes["GET /v1/legal/consent"] = {
      status: 200,
      body: { privacy_version: 3, accepted_version: null, ads_enabled: false },
    };
    routes["POST /v1/legal/consent"] = {
      status: 200,
      body: { document: "privacy", accepted_version: 3 },
    };
    render(<AgentThread agent={NEWS} />);
    expect(
      await screen.findByText("Sponsored placements — with your say"),
    ).toBeTruthy();
    expect(screen.queryByText("Sponsored")).toBeNull();

    // Accepting names the CURRENT version and reloads the slot — which
    // now serves a placement, label first, and posts its impression.
    routes["GET /v1/press/ads"] = {
      status: 200,
      body: {
        placement: {
          placement_id: "pl1",
          label: "Sponsored",
          campaign_name: "Kettle week",
          creative: "Kettles for every kitchen.",
          offer_ref: "listing-42",
          advertiser: "shop",
          surface: "edition",
          content_ref: "st1",
          breakdown: {},
        },
      },
    };
    routes["POST /v1/adhouse/placements/pl1/impression"] = {
      status: 200,
      body: { recorded: true, kind: "impression" },
    };
    fireEvent.click(screen.getByText("Accept version 3"));
    expect(await screen.findByText("Sponsored")).toBeTruthy();
    expect(screen.getByText("Kettle week")).toBeTruthy();
    const accepted = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/legal/consent",
    );
    expect(accepted?.body).toMatchObject({ document: "privacy", version: 3 });
    const impression = calls.find(
      (c) =>
        c.method === "POST" &&
        c.path === "/v1/adhouse/placements/pl1/impression",
    );
    expect(impression).toBeTruthy();

    // The affiliate tap delivers the click on the provenance token.
    routes["POST /v1/adhouse/placements/pl1/click"] = {
      status: 200,
      body: { recorded: true, kind: "click" },
    };
    fireEvent.click(screen.getByText("View the offer"));
    expect(
      calls.some(
        (c) =>
          c.method === "POST" &&
          c.path === "/v1/adhouse/placements/pl1/click",
      ),
    ).toBeTruthy();
  });

  it("compares on verified evidence and crowns a deterministic best buy", async () => {
    const EXPLORER: RosterAgent = {
      ...NEWS,
      agent_id: "explorer",
      name: "Explorer",
    };
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/explorer/compare"] = {
      status: 200,
      body: {
        category: "",
        rows: [
          {
            listing_id: "steel",
            title: "Steel kettle",
            seller: "shop",
            price_micros: 20_000_000,
            currency: "USD",
            list_price_micros: 25_000_000,
            discount_percent: 20,
            feedback: { count: 3, mean: 4.3, factor: 0.86 },
            trust: { score: 0.9, basis: "derived from the order book" },
            lab: { count: 1, mean_score: 92, factor: 0.92 },
            eligible: true,
            gaps: [],
          },
          {
            listing_id: "glass",
            title: "Glass kettle",
            seller: "shop",
            price_micros: 30_000_000,
            currency: "USD",
            list_price_micros: null,
            discount_percent: null,
            feedback: { count: 0, mean: null, factor: 0.5 },
            trust: { score: 0.5, basis: "no order history" },
            lab: { count: 0, mean_score: null, factor: 0.5 },
            eligible: false,
            gaps: ["out of stock"],
          },
        ],
        brief: {
          mode: "balanced",
          winner_listing_id: "steel",
          ranked: [
            {
              listing_id: "steel",
              title: "Steel kettle",
              seller: "shop",
              score: 0.81,
              factors: { price: 1, discount: 0.2, feedback: 0.86, trust: 0.9, lab: 0.92 },
              weights: {},
            },
          ],
        },
      },
    };
    routes["POST /v1/explorer/interests"] = {
      status: 200,
      body: { interest: { schedule_id: "s1" } },
    };
    render(<AgentThread agent={EXPLORER} />);

    // The winner banner with the reasons on demand.
    expect(await screen.findByText("Best buy")).toBeTruthy();
    fireEvent.click(screen.getByText("Why this pick"));
    expect(screen.getByText(/trust: 0.9/)).toBeTruthy();
    // The discount renders only where it is a FACT; the ineligible row
    // stays visible with its gap named.
    expect(screen.getByText("−20%")).toBeTruthy();
    expect(screen.getByText("out of stock")).toBeTruthy();
    expect(screen.getByText(/3 verified reviews/)).toBeTruthy();

    // A mode chip refetches with the mode named.
    fireEvent.click(screen.getByRole("button", { name: "Measured" }));
    const modes = calls.filter(
      (c) => c.method === "GET" && c.path === "/v1/explorer/compare",
    );
    expect(modes.length).toBeGreaterThanOrEqual(2);

    // Following schedules the daily brief.
    fireEvent.click(screen.getByText("Follow this"));
    expect(await screen.findByText("Following ✓")).toBeTruthy();
    const followed = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/explorer/interests",
    );
    expect(followed?.body).toMatchObject({ mode: "measured" });
  });

  it("plans a trip: feasible ranks, broken constraints carry names", async () => {
    const TRAVEL: RosterAgent = {
      ...NEWS,
      agent_id: "travel",
      name: "Travel Plan",
    };
    routes["GET /v1/records/calendar"] = {
      status: 200,
      body: {
        items: [
          {
            event_id: "e1",
            title: "Trip: Coast package",
            starts_at: "2026-08-01T00:00:00Z",
            ends_at: "2026-08-04T00:00:00Z",
            source: "trip",
          },
        ],
      },
    };
    routes["GET /v1/travel/plan"] = {
      status: 200,
      body: {
        mode: "balanced",
        nights: 3,
        party: ["alice", "bob"],
        budget_micros: 100_000_000,
        open_slots: [["2026-08-01T00:00:00Z", "2026-08-06T00:00:00Z"]],
        feasible: [
          {
            listing_id: "coast",
            title: "Coast package",
            seller: "shop",
            price_micros: 20_000_000,
            party_cost_micros: 40_000_000,
            score: 0.7,
            factors: { price: 1, trust: 0.5 },
            feasible: true,
            violations: [],
          },
        ],
        infeasible: [
          {
            listing_id: "alpine",
            title: "Alpine package",
            seller: "shop",
            price_micros: 90_000_000,
            party_cost_micros: 180_000_000,
            score: 0.4,
            factors: { price: 0.2, trust: 0.5 },
            feasible: false,
            violations: ["over budget by 80.00 USD for 2 travellers"],
          },
        ],
      },
    };
    render(<AgentThread agent={TRAVEL} />);

    // The calendar renders, the booked trip marked as one.
    expect(await screen.findByText(/Trip: Coast package/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("From"), {
      target: { value: "2026-08-01" },
    });
    fireEvent.change(screen.getByLabelText("To"), {
      target: { value: "2026-08-14" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Plan" }));

    // The feasible plan ranks with its reasons; the broken constraint
    // renders by name, never silently buried.
    expect(await screen.findByText(/★ Coast package/)).toBeTruthy();
    expect(
      screen.getByText("over budget by 80.00 USD for 2 travellers"),
    ).toBeTruthy();
    const planned = calls.find(
      (c) => c.method === "GET" && c.path === "/v1/travel/plan",
    );
    expect(planned).toBeTruthy();
  });

  it("stays silent on hosts from before the press", async () => {
    routes["GET /v1/press/genres"] = { status: 404, body: {} };
    render(<AgentThread agent={NEWS} />);
    // The agent's welcome still stands; no press chrome renders.
    expect(
      await screen.findByText(/I collect member contributions\./),
    ).toBeTruthy();
    expect(screen.queryByText("Contributions")).toBeNull();
  });
});
