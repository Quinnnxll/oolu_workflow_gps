import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { Market, RequestsPane, SellPane, money, parseAttributes } from "./Market";
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
  function orderIn(
    state: string,
    extra: { escrow_held?: boolean; charge_ref?: string | null } = {},
  ) {
    return {
      order: {
        order_id: "o1",
        buyer_principal: "user-1",
        seller_id: "acme",
        seller_principal: "",
        offer,
        tracking: "",
        delivery_evidence: "",
        escrow_held: extra.escrow_held ?? false,
        charge_ref: extra.charge_ref ?? null,
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

  it("delivers with the evidence typed on the card", async () => {
    routes["GET /v1/commerce/orders"] = {
      status: 200,
      body: { items: [orderIn("fulfilling")] },
    };
    routes["POST /v1/commerce/orders/o1/deliver"] = {
      status: 200,
      body: orderIn("delivered", { escrow_held: true, charge_ref: "ch_1" }),
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    fireEvent.change(
      (await screen.findByText("Delivery evidence")).querySelector("input")!,
      { target: { value: "signed-photo" } },
    );
    fireEvent.click(screen.getByText("Mark delivered"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/commerce/orders/o1/deliver" &&
            (c.body as { evidence: string }).evidence === "signed-photo",
        ),
      ).toBe(true),
    );
  });

  it("names the escrow exception and heals it through the evidence door", async () => {
    routes["GET /v1/commerce/orders"] = {
      status: 200,
      body: {
        items: [orderIn("delivered", { escrow_held: true, charge_ref: null })],
      },
    };
    routes["POST /v1/commerce/orders/o1/evidence"] = {
      status: 200,
      body: orderIn("delivered", { escrow_held: true, charge_ref: "ch_1" }),
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    await screen.findByText(/Escrow stays held/);
    // No acceptance offered while the exception stands.
    expect(screen.queryByText(/Accept —/)).toBeNull();
    fireEvent.change(
      screen.getByText("Delivery evidence").querySelector("input")!,
      { target: { value: "late-photo" } },
    );
    fireEvent.click(screen.getByText("Attach evidence"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/commerce/orders/o1/evidence" &&
            (c.body as { evidence: string }).evidence === "late-photo",
        ),
      ).toBe(true),
    );
  });

  it("names release for escrow-held acceptance and shows the invoice", async () => {
    routes["GET /v1/commerce/orders"] = {
      status: 200,
      body: {
        items: [orderIn("delivered", { escrow_held: true, charge_ref: "ch_1" })],
      },
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    await screen.findByText("Accept — release escrow");
    await screen.findByText("held in escrow");

    cleanup();
    routes["GET /v1/commerce/orders"] = {
      status: 200,
      body: {
        items: [orderIn("completed", { escrow_held: true, charge_ref: "ch_1" })],
      },
    };
    routes["GET /v1/commerce/orders/o1/invoice"] = {
      status: 200,
      body: {
        number: "US-000001",
        order_id: "o1",
        subtotal_micros: 100_000_000,
        fees_micros: 0,
        tax_micros: 8_000_000,
        total_micros: 108_000_000,
        currency: "USD",
        issued_at: "2026-07-01T12:00:00+00:00",
      },
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    fireEvent.click(await screen.findByText("Invoice"));
    await screen.findByText(/US-000001/);
  });
});

describe("RequestsPane: RFQ discovery never bypasses the law", () => {
  it("parses attribute lines", () => {
    expect(parseAttributes("material=steel\nvolume = 1l\nnoise")).toEqual({
      material: "steel",
      volume: "1l",
    });
  });

  it("opens a typed request through the door", async () => {
    routes["GET /v1/commerce/rfqs"] = { status: 200, body: { items: [] } };
    routes["POST /v1/commerce/rfqs"] = {
      status: 201,
      body: {
        rfq_id: "r1",
        buyer_principal: "user-1",
        specification: {
          category: "household",
          required_attributes: [["material", "steel"]],
          quantity: 2,
          destination_reference: "",
        },
        state: "open",
        expires_at: "2026-07-08T12:00:00+00:00",
      },
    };
    render(<RequestsPane onAwardBuy={() => {}} />);
    fireEvent.change(
      (await screen.findByText("Category")).querySelector("input")!,
      { target: { value: "household" } },
    );
    fireEvent.change(screen.getByText("Quantity").querySelector("input")!, {
      target: { value: "2" },
    });
    fireEvent.change(
      screen
        .getByText("Required attributes (key=value per line)")
        .querySelector("textarea")!,
      { target: { value: "material=steel" } },
    );
    fireEvent.click(screen.getByText("Open request"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/commerce/rfqs" &&
            (c.body as { required_attributes: Record<string, string> })
              .required_attributes.material === "steel",
        ),
      ).toBe(true),
    );
  });

  it("marks substitutes and awards only the eligible into the buy path", async () => {
    routes["GET /v1/commerce/rfqs"] = {
      status: 200,
      body: {
        items: [
          {
            rfq_id: "r1",
            buyer_principal: "user-1",
            specification: {
              category: "household",
              required_attributes: [["material", "steel"]],
              quantity: 1,
              destination_reference: "",
            },
            state: "open",
            expires_at: "2026-07-08T12:00:00+00:00",
          },
        ],
      },
    };
    routes["GET /v1/commerce/rfqs/r1/quotes"] = {
      status: 200,
      body: {
        items: [
          {
            quote_id: "q1",
            rfq_id: "r1",
            seller_principal: "seller-1",
            attributes: [["material", "steel"]],
            offer,
            eligible: true,
            ineligible_reasons: [],
          },
          {
            quote_id: "q2",
            rfq_id: "r1",
            seller_principal: "seller-2",
            attributes: [["material", "plastic"]],
            offer: { ...offer, offer_id: "of-2", seller_id: "planet-plastics" },
            eligible: false,
            ineligible_reasons: [
              "attribute 'material' is 'plastic', not the required 'steel'",
            ],
          },
        ],
      },
    };
    routes["POST /v1/commerce/rfqs/r1/award"] = { status: 200, body: offer };
    const awarded: string[] = [];
    render(
      <RequestsPane
        onAwardBuy={(won, category) => awarded.push(`${won.offer_id}:${category}`)}
      />,
    );
    fireEvent.click(await screen.findByText("Quotes"));
    await screen.findByText("substitute");
    screen.getByText(/not the required 'steel'/);
    // Exactly one Award button: the substitute never offers one.
    const buttons = screen.getAllByText("Award & buy");
    expect(buttons.length).toBe(1);
    fireEvent.click(buttons[0]);
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" && c.path === "/v1/commerce/rfqs/r1/award",
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(awarded).toEqual(["l1:v1:household"]));
  });
});

describe("Market: milestones and standing obligations", () => {
  const serviceOffer = {
    ...offer,
    milestones: [
      ["deposit", 40_000_000],
      ["balance", 60_000_000],
    ] as [string, number][],
  };

  function serviceOrder(state: string) {
    return {
      order: {
        order_id: "o1",
        buyer_principal: "user-1",
        seller_id: "acme",
        seller_principal: "",
        offer: serviceOffer,
        tracking: "",
        delivery_evidence: "",
        escrow_held: true,
        charge_ref: "ch_1",
        created_at: "2026-07-01T12:00:00+00:00",
      },
      state,
    };
  }

  it("walks a tranche through deliver and accept over the doors", async () => {
    routes["GET /v1/commerce/orders"] = {
      status: 200,
      body: { items: [serviceOrder("fulfilling")] },
    };
    routes["GET /v1/commerce/orders/o1/milestones"] = {
      status: 200,
      body: {
        items: [
          {
            order_id: "o1",
            index: 0,
            title: "deposit",
            amount_micros: 40_000_000,
            state: "pending",
            evidence: "",
          },
          {
            order_id: "o1",
            index: 1,
            title: "balance",
            amount_micros: 60_000_000,
            state: "delivered",
            evidence: "doc",
          },
        ],
      },
    };
    routes["POST /v1/commerce/orders/o1/milestones/0/deliver"] = {
      status: 200,
      body: {},
    };
    routes["POST /v1/commerce/orders/o1/milestones/1/accept"] = {
      status: 200,
      body: {},
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    fireEvent.click(await screen.findByText("Milestones"));
    await screen.findByText(/deposit/);
    fireEvent.change(screen.getByPlaceholderText("evidence"), {
      target: { value: "deposit-doc" },
    });
    fireEvent.click(screen.getByText("Deliver"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/commerce/orders/o1/milestones/0/deliver" &&
            (c.body as { evidence: string }).evidence === "deposit-doc",
        ),
      ).toBe(true),
    );
    fireEvent.click(screen.getByText("Accept tranche"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/commerce/orders/o1/milestones/1/accept",
        ),
      ).toBe(true),
    );
  });

  it("renews a standing obligation through the intent door and places it", async () => {
    routes["GET /v1/commerce/recurring"] = {
      status: 200,
      body: {
        items: [
          {
            obligation_id: "ob1",
            offer,
            period_days: 30,
            state: "active",
            renewals: 2,
            category: "household",
          },
        ],
      },
    };
    routes["POST /v1/commerce/recurring/ob1/renew"] = {
      status: 201,
      body: {
        intent: { intent_id: "i-renewal", offer_snapshot: offer },
        state: "auto_approved",
        intent_digest: "d".repeat(64),
        verdict: { decision: "auto_execute", reasons: ["renewal"] },
      },
    };
    routes["POST /v1/commerce/orders"] = {
      status: 201,
      body: { order: { order_id: "o9", offer }, state: "confirmed" },
    };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    await screen.findByText(/Standing: l1/);
    fireEvent.click(screen.getByText("Renew now"));
    await waitFor(() => {
      const posts = calls.filter((c) => c.method === "POST");
      expect(posts.map((c) => c.path)).toEqual([
        "/v1/commerce/recurring/ob1/renew",
        "/v1/commerce/orders",
      ]);
      expect(
        (posts[1].body as { intent_id: string }).intent_id,
      ).toBe("i-renewal");
    });
  });

  it("cancels an obligation so nothing renews after", async () => {
    routes["GET /v1/commerce/recurring"] = {
      status: 200,
      body: {
        items: [
          {
            obligation_id: "ob1",
            offer,
            period_days: 30,
            state: "active",
            renewals: 0,
            category: "household",
          },
        ],
      },
    };
    routes["DELETE /v1/commerce/recurring/ob1"] = { status: 200, body: {} };
    render(<Market />);
    fireEvent.click(await screen.findByText(/Orders/));
    fireEvent.click(await screen.findByText("Cancel"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "DELETE" && c.path === "/v1/commerce/recurring/ob1",
        ),
      ).toBe(true),
    );
  });
});

describe("SellPane: the signed boundary", () => {
  it("loads the sales policy and signs the new floors in micros", async () => {
    routes["GET /v1/commerce/seller/kyc"] = {
      status: 200,
      body: { status: "verified", legal_name: "Acme GmbH" },
    };
    routes["GET /v1/commerce/listings"] = { status: 200, body: { items: [] } };
    routes["GET /v1/commerce/sales-policy"] = {
      status: 200,
      body: {
        policy_id: "default-sales",
        policy_version: "sales-v1",
        price_floor_micros: 0,
        absolute_floor_micros: 50_000_000,
        auto_accept_price_micros: 90_000_000,
        maximum_discount_percent: 20,
        automated_quantity_limit: 10,
        seller_review_limit_micros: 1_000_000_000,
      },
    };
    routes["PUT /v1/commerce/sales-policy"] = { status: 200, body: {} };
    render(<SellPane onChanged={() => {}} />);
    const floor = (
      await screen.findByText("Absolute floor (USD/unit)")
    ).querySelector("input")!;
    await waitFor(() => expect(floor.value).toBe("50"));
    fireEvent.change(floor, { target: { value: "60" } });
    fireEvent.click(screen.getByText("Sign boundary"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "PUT" &&
            c.path === "/v1/commerce/sales-policy" &&
            (c.body as { absolute_floor_micros: number })
              .absolute_floor_micros === 60_000_000,
        ),
      ).toBe(true),
    );
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
