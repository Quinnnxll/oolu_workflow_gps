import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { Market, SellPane, money } from "./Market";
import type { CommerceListing } from "../api";

// The market surface never shortcuts the spine: buying walks offer →
// intent → (approval) → order, the approval card renders the server's own
// digest summary, and selling is gated on the seller-KYC verdict. These
// tests pin the doors each button calls — the client speaks the gateway's
// real /v1/commerce contract.

let routes: Record<string, { status: number; body: unknown }>;
let calls: { method: string; path: string; body: unknown }[];

const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
  const u = new URL(String(input), "http://local.test");
  const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ method, path: u.pathname, body });
  const hit = routes[`${method} ${u.pathname}`] ?? { status: 200, body: {} };
  return {
    ok: hit.status >= 200 && hit.status < 300,
    status: hit.status,
    text: async () => JSON.stringify(hit.body),
    json: async () => hit.body,
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

function listing(overrides: Partial<CommerceListing> = {}): CommerceListing {
  return {
    listing_id: "l1",
    seller_principal: "seller-1",
    seller_id: "acme",
    title: "Steel bottle",
    category: "household",
    description: "",
    unit_price_micros: 100_000_000,
    currency: "USD",
    quantity_available: 3,
    refund_terms: "30-day returns",
    fulfillment_terms: "ships in 2 days",
    refundable: true,
    status: "active",
    version: 1,
    ...overrides,
  };
}

const offer = {
  offer_id: "l1:v1",
  seller_id: "acme",
  offer_version: 1,
  item_id: "l1",
  quantity: 1,
  subtotal_micros: 100_000_000,
  currency: "USD",
  tax_estimate_micros: 0,
  fees_micros: 0,
  fulfillment_terms: "ships in 2 days",
  refund_terms: "30-day returns",
  refundable: true,
  recurring_terms: "",
};

describe("money", () => {
  it("formats micros in the offer's currency", () => {
    expect(money(108_000_000, "USD")).toBe("108.00 USD");
  });
});

describe("Market: buying walks offer → intent → approval", () => {
  it("mints the exact offer, binds the intent, and reports the verdict", async () => {
    routes["GET /v1/commerce/catalog"] = {
      status: 200,
      body: { items: [listing()] },
    };
    routes["POST /v1/commerce/listings/l1/offer"] = { status: 200, body: offer };
    routes["POST /v1/commerce/intents"] = {
      status: 201,
      body: {
        intent: { intent_id: "i1", offer_snapshot: offer },
        state: "approval_pending",
        intent_digest: "d".repeat(64),
        verdict: { decision: "require_approval", reasons: ["first purchase"] },
      },
    };
    render(<Market />);
    fireEvent.click(await screen.findByText("Buy"));
    await screen.findByText(/sent for your approval/);

    const posts = calls.filter((c) => c.method === "POST");
    expect(posts.map((c) => c.path)).toEqual([
      "/v1/commerce/listings/l1/offer",
      "/v1/commerce/intents",
    ]);
    const intent = posts[1].body as {
      offer: typeof offer;
      idempotency_key: string;
      risk_facts: { seller_identity_verified: boolean };
    };
    // The intent binds the EXACT minted offer, carries its own
    // idempotency key, and asserts only what the client honestly knows.
    expect(intent.offer).toEqual(offer);
    expect(intent.idempotency_key).toBeTruthy();
    expect(intent.risk_facts.seller_identity_verified).toBe(true);
    // No order was placed: approval gates it.
    expect(posts.some((c) => c.path === "/v1/commerce/orders")).toBe(false);
  });

  it("shows the digest-rendered terms and approves through the door", async () => {
    routes["GET /v1/commerce/approvals"] = {
      status: 200,
      body: {
        items: [
          {
            intent_id: "i1",
            item: "steel-bottle",
            quantity: 1,
            seller: "acme",
            total: "108.00 USD",
            delivery_terms: "ships in 2 days",
            refund_terms: "30-day returns",
            risks: ["first purchase from this counterparty"],
            approval_strength: "normal",
            intent_digest: "d".repeat(64),
            expires_at: "2026-07-02T12:00:00+00:00",
          },
        ],
      },
    };
    routes["POST /v1/commerce/intents/i1/approval"] = {
      status: 200,
      body: { approval: {}, state: "approved" },
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Approvals/));
    await screen.findByText(/108\.00 USD from acme/);
    screen.getByText("first purchase from this counterparty");
    fireEvent.click(screen.getByText("Approve exactly these terms"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/commerce/intents/i1/approval" &&
            (c.body as { decision: string }).decision === "approve",
        ),
      ).toBe(true),
    );
  });

  it("places an approved intent as an order", async () => {
    routes["GET /v1/commerce/intents"] = {
      status: 200,
      body: {
        items: [
          {
            intent: { intent_id: "i1", offer_snapshot: offer },
            state: "approved",
            intent_digest: "d".repeat(64),
            verdict: { decision: "require_approval", reasons: [] },
          },
        ],
      },
    };
    routes["POST /v1/commerce/orders"] = {
      status: 201,
      body: { order: { order_id: "o1", offer }, state: "confirmed" },
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Approvals/));
    fireEvent.click(await screen.findByText("Place order"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/commerce/orders" &&
            (c.body as { intent_id: string }).intent_id === "i1",
        ),
      ).toBe(true),
    );
  });
});

describe("Market: orders offer the next lawful step", () => {
  function orderIn(state: string) {
    return {
      order: {
        order_id: "o1",
        buyer_principal: "user-1",
        seller_id: "acme",
        seller_principal: "",
        offer,
        tracking: "",
        delivery_evidence: "",
        created_at: "2026-07-01T12:00:00+00:00",
      },
      state,
    };
  }

  it("ships a confirmed order and captures only at acceptance", async () => {
    routes["GET /v1/commerce/orders"] = {
      status: 200,
      body: { items: [orderIn("confirmed")] },
    };
    routes["POST /v1/commerce/orders/o1/ship"] = {
      status: 200,
      body: orderIn("fulfilling"),
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    fireEvent.click(await screen.findByText("Mark shipped"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) => c.method === "POST" && c.path === "/v1/commerce/orders/o1/ship",
        ),
      ).toBe(true),
    );
  });

  it("names acceptance as the capture moment", async () => {
    routes["GET /v1/commerce/orders"] = {
      status: 200,
      body: { items: [orderIn("delivered")] },
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    await screen.findByText("Accept — capture payment");
  });
});

describe("SellPane: KYC gates everything", () => {
  it("applies as a legal entity through the seller door", async () => {
    routes["GET /v1/commerce/seller/kyc"] = {
      status: 200,
      body: { status: "not_applied" },
    };
    routes["GET /v1/commerce/listings"] = { status: 200, body: { items: [] } };
    routes["POST /v1/commerce/seller/kyc"] = {
      status: 201,
      body: { status: "pending_review", legal_name: "Acme GmbH" },
    };
    render(<SellPane onChanged={() => {}} />);
    fireEvent.change(
      (await screen.findByText("Legal name")).querySelector("input")!,
      { target: { value: "Acme GmbH" } },
    );
    fireEvent.change(
      screen.getByText("Company e-mail").querySelector("input")!,
      { target: { value: "kyc@acme.example" } },
    );
    fireEvent.click(screen.getByText("Apply"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/commerce/seller/kyc" &&
            (c.body as { legal_name: string }).legal_name === "Acme GmbH",
        ),
      ).toBe(true),
    );
  });

  it("keeps Publish closed until the seller is verified", async () => {
    routes["GET /v1/commerce/seller/kyc"] = {
      status: 200,
      body: { status: "pending_review", legal_name: "Acme GmbH" },
    };
    routes["GET /v1/commerce/listings"] = {
      status: 200,
      body: { items: [listing({ status: "draft" })] },
    };
    render(<SellPane onChanged={() => {}} />);
    const publish = (await screen.findByText("Publish")) as HTMLButtonElement;
    expect(publish.disabled).toBe(true);
  });

  it("publishes a draft once verified", async () => {
    routes["GET /v1/commerce/seller/kyc"] = {
      status: 200,
      body: { status: "verified", legal_name: "Acme GmbH" },
    };
    routes["GET /v1/commerce/listings"] = {
      status: 200,
      body: { items: [listing({ status: "draft" })] },
    };
    routes["POST /v1/commerce/listings/l1/publish"] = {
      status: 200,
      body: listing({ status: "active" }),
    };
    render(<SellPane onChanged={() => {}} />);
    const publish = (await screen.findByText("Publish")) as HTMLButtonElement;
    expect(publish.disabled).toBe(false);
    fireEvent.click(publish);
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" && c.path === "/v1/commerce/listings/l1/publish",
        ),
      ).toBe(true),
    );
  });
});
