import { useCallback, useEffect, useState } from "react";
import { api, session } from "../api";
import type {
  CommerceApproval,
  CommerceIntent,
  CommerceInvoice,
  CommerceLedgerTxn,
  CommerceListing,
  CommerceOffer,
  CommerceOrder,
  CommerceQuote,
  CommerceRfq,
  CommerceSalesPolicy,
  SellerKycView,
} from "../api";

// The market surface (marketplace-build-plan M1+M2): buying walks the
// spine's law — offer → intent → verdict → (approval) → order — and this
// screen never shortcuts it: every button is one of the gateway's doors,
// the approval card shows the server's own digest-rendered summary, and
// the escrow states are named for what they are: money waiting for the
// world to confirm.

export function money(micros: number, currency: string): string {
  return `${(micros / 1_000_000).toFixed(2)} ${currency}`;
}

// "key=value" lines → the attributes object the RFQ doors speak.
export function parseAttributes(text: string): Record<string, string> {
  const attributes: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const [key, ...rest] = line.split("=");
    if (key.trim() && rest.length) {
      attributes[key.trim()] = rest.join("=").trim();
    }
  }
  return attributes;
}

function offerTotal(offer: CommerceOffer): number {
  return offer.subtotal_micros + offer.tax_estimate_micros + offer.fees_micros;
}

type Tab = "shop" | "requests" | "approvals" | "orders" | "sell";

export function Market() {
  const [tab, setTab] = useState<Tab>("shop");
  const [catalog, setCatalog] = useState<CommerceListing[]>([]);
  const [approvals, setApprovals] = useState<CommerceApproval[]>([]);
  const [ready, setReady] = useState<CommerceIntent[]>([]);
  const [orders, setOrders] = useState<CommerceOrder[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [cat, inbox, approved, mine] = await Promise.all([
        api.commerceCatalog(),
        api.commerceApprovals(),
        api.commerceIntents("approved"),
        api.commerceOrders(),
      ]);
      setCatalog(cat.items ?? []);
      setApprovals(inbox.items ?? []);
      setReady(approved.items ?? []);
      setOrders(mine.items ?? []);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const act = useCallback(
    async (work: () => Promise<string>) => {
      setError("");
      try {
        setNotice(await work());
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [refresh],
  );

  // One buy path for every offer source (shelf or awarded quote): bind the
  // intent, read the verdict, place only what the law already allows.
  const buyOffer = useCallback(
    async (offer: CommerceOffer, category: string): Promise<string> => {
      const intent = await api.commerceIntentCreate(offer, category);
      if (intent.state === "denied") {
        return `refused: ${intent.verdict.reasons.join("; ")}`;
      }
      if (intent.state === "approval_pending") {
        return "sent for your approval — the exact terms are in Approvals";
      }
      await api.commerceOrderPlace(intent.intent.intent_id);
      return `order placed — ${money(
        offerTotal(offer),
        offer.currency,
      )} authorized, captured only against delivery`;
    },
    [],
  );

  const buy = useCallback(
    (listing: CommerceListing) =>
      act(async () => {
        const offer = await api.commerceListingOffer(listing.listing_id, 1);
        return buyOffer(offer, listing.category);
      }),
    [act, buyOffer],
  );

  return (
    <div className="market">
      <nav className="dev-nav">
        <button className={tab === "shop" ? "on" : ""} onClick={() => setTab("shop")}>
          Shop
        </button>
        <button
          className={tab === "requests" ? "on" : ""}
          onClick={() => setTab("requests")}
        >
          Requests
        </button>
        <button
          className={tab === "approvals" ? "on" : ""}
          onClick={() => setTab("approvals")}
        >
          Approvals
          {approvals.length + ready.length ? (
            <span className="badge">{approvals.length + ready.length}</span>
          ) : null}
        </button>
        <button
          className={tab === "orders" ? "on" : ""}
          onClick={() => setTab("orders")}
        >
          Orders
          {orders.length ? <span className="badge">{orders.length}</span> : null}
        </button>
        <button className={tab === "sell" ? "on" : ""} onClick={() => setTab("sell")}>
          Sell
        </button>
      </nav>
      {notice && <p className="hint">{notice}</p>}
      {error && <p className="error">{error}</p>}
      {tab === "shop" && <Shop catalog={catalog} onBuy={buy} />}
      {tab === "requests" && (
        <RequestsPane
          onAwardBuy={(offer, category) => act(() => buyOffer(offer, category))}
        />
      )}
      {tab === "approvals" && (
        <ApprovalsPane
          approvals={approvals}
          ready={ready}
          onDecide={(id, approve) =>
            act(async () => {
              const decided = await api.commerceDecide(id, approve);
              return approve ? `approved — state ${decided.state}` : "declined";
            })
          }
          onPlace={(id) =>
            act(async () => {
              const order = await api.commerceOrderPlace(id);
              return `order placed (${order.state})`;
            })
          }
        />
      )}
      {tab === "orders" && <OrdersPane orders={orders} act={act} />}
      {tab === "sell" && <SellPane onChanged={refresh} />}
    </div>
  );
}

function Shop({
  catalog,
  onBuy,
}: {
  catalog: CommerceListing[];
  onBuy: (listing: CommerceListing) => void;
}) {
  if (!catalog.length) {
    return <p className="empty">Nothing is listed yet.</p>;
  }
  return (
    <div className="market-grid">
      {catalog.map((listing) => (
        <div className="order-card" key={listing.listing_id}>
          <strong>{listing.title}</strong>
          {listing.description && <p className="muted">{listing.description}</p>}
          <p>
            {money(listing.unit_price_micros, listing.currency)}
            <span className="muted"> · {listing.quantity_available} available</span>
          </p>
          <p className="muted">
            {listing.fulfillment_terms} · {listing.refund_terms}
          </p>
          <button onClick={() => onBuy(listing)}>Buy</button>
        </div>
      ))}
    </div>
  );
}

// The RFQ surface (M2): open a typed request, compare normalized quotes —
// substitutes marked with their gaps — award one, and buy it through the
// same intent door as everything else.
export function RequestsPane({
  onAwardBuy,
}: {
  onAwardBuy: (offer: CommerceOffer, category: string) => void;
}) {
  const [requests, setRequests] = useState<CommerceRfq[]>([]);
  const [quotes, setQuotes] = useState<Record<string, CommerceQuote[]>>({});
  const [category, setCategory] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [attributesText, setAttributesText] = useState("");
  const [quotePrice, setQuotePrice] = useState("");
  const [quoteItem, setQuoteItem] = useState("");
  const [quoteAttributes, setQuoteAttributes] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setRequests((await api.commerceRfqs()).items ?? []);
  }, []);

  useEffect(() => {
    void refresh().catch(() => {});
  }, [refresh]);

  const run = useCallback(
    async (work: () => Promise<string>) => {
      setError("");
      try {
        setNotice(await work());
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [refresh],
  );

  const loadQuotes = useCallback(async (rfqId: string) => {
    const { items } = await api.commerceRfqQuotes(rfqId);
    setQuotes((held) => ({ ...held, [rfqId]: items ?? [] }));
  }, []);

  return (
    <div className="market-grid">
      <div className="order-card">
        <strong>Ask the market</strong>
        <p className="muted">
          Name what you require; quotes that miss a requirement are marked
          as substitutes and can never be awarded.
        </p>
        <label className="field">
          Category
          <input value={category} onChange={(e) => setCategory(e.target.value)} />
        </label>
        <label className="field">
          Quantity
          <input value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </label>
        <label className="field">
          Required attributes (key=value per line)
          <textarea
            value={attributesText}
            onChange={(e) => setAttributesText(e.target.value)}
          />
        </label>
        <button
          onClick={() =>
            run(async () => {
              await api.commerceRfqOpen({
                category,
                quantity: Number(quantity) || 1,
                required_attributes: parseAttributes(attributesText),
              });
              return "request opened — sellers can quote it now";
            })
          }
        >
          Open request
        </button>
      </div>
      {requests.map((rfq) => (
        <div className="order-card" key={rfq.rfq_id}>
          <strong>
            {rfq.specification.quantity} × {rfq.specification.category}
          </strong>{" "}
          <span className="chip">{rfq.state}</span>
          {rfq.specification.required_attributes.length > 0 && (
            <ul className="muted">
              {rfq.specification.required_attributes.map(([key, value]) => (
                <li key={key}>
                  {key} = {value}
                </li>
              ))}
            </ul>
          )}
          <button className="linklike" onClick={() => void loadQuotes(rfq.rfq_id)}>
            Quotes
          </button>
          {(quotes[rfq.rfq_id] ?? []).map((quote) => (
            <div key={quote.quote_id}>
              <p>
                {money(offerTotal(quote.offer), quote.offer.currency)}{" "}
                <span className="muted">from {quote.offer.seller_id}</span>{" "}
                {!quote.eligible && <span className="chip">substitute</span>}
              </p>
              {quote.eligible ? (
                <button
                  onClick={() =>
                    run(async () => {
                      const offer = await api.commerceRfqAward(
                        rfq.rfq_id,
                        quote.quote_id,
                      );
                      onAwardBuy(offer, rfq.specification.category);
                      return "awarded — the offer heads to the intent door";
                    })
                  }
                >
                  Award &amp; buy
                </button>
              ) : (
                <ul className="muted">
                  {quote.ineligible_reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              )}
            </div>
          ))}
          {rfq.state === "open" && (
            <>
              <p className="muted">Quote this request:</p>
              <label className="field">
                Item
                <input
                  value={quoteItem}
                  onChange={(e) => setQuoteItem(e.target.value)}
                />
              </label>
              <label className="field">
                Price per unit (USD)
                <input
                  value={quotePrice}
                  onChange={(e) => setQuotePrice(e.target.value)}
                />
              </label>
              <label className="field">
                Attributes (key=value per line)
                <textarea
                  value={quoteAttributes}
                  onChange={(e) => setQuoteAttributes(e.target.value)}
                />
              </label>
              <button
                onClick={() =>
                  run(async () => {
                    const unit = Math.round(Number(quotePrice) * 1_000_000);
                    await api.commerceQuoteSubmit(
                      rfq.rfq_id,
                      {
                        offer_id: `quote:${rfq.rfq_id}:${Date.now()}`,
                        seller_id: session.principal ?? "seller",
                        offer_version: 1,
                        item_id: quoteItem || rfq.specification.category,
                        quantity: rfq.specification.quantity,
                        subtotal_micros: unit * rfq.specification.quantity,
                        currency: "USD",
                        tax_estimate_micros: 0,
                        fees_micros: 0,
                        fulfillment_terms: "standard shipping",
                        refund_terms: "30-day returns",
                        refundable: true,
                        recurring_terms: "",
                      },
                      parseAttributes(quoteAttributes),
                    );
                    await loadQuotes(rfq.rfq_id);
                    return "quote filed — your signed floor already gated it";
                  })
                }
              >
                Submit quote
              </button>
            </>
          )}
        </div>
      ))}
      {notice && <p className="hint">{notice}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function ApprovalsPane({
  approvals,
  ready,
  onDecide,
  onPlace,
}: {
  approvals: CommerceApproval[];
  ready: CommerceIntent[];
  onDecide: (intentId: string, approve: boolean) => void;
  onPlace: (intentId: string) => void;
}) {
  if (!approvals.length && !ready.length) {
    return <p className="empty">Nothing awaits your decision.</p>;
  }
  return (
    <div className="market-grid">
      {approvals.map((item) => (
        <div className="order-card" key={item.intent_id}>
          <strong>
            {item.quantity} × {item.item}
          </strong>
          <p>
            {item.total} from {item.seller}
          </p>
          <p className="muted">
            {item.delivery_terms} · {item.refund_terms}
          </p>
          <ul className="muted">
            {item.risks.map((risk) => (
              <li key={risk}>{risk}</li>
            ))}
          </ul>
          <p className="muted">digest {item.intent_digest.slice(0, 12)}…</p>
          <button onClick={() => onDecide(item.intent_id, true)}>
            Approve exactly these terms
          </button>{" "}
          <button className="linklike" onClick={() => onDecide(item.intent_id, false)}>
            Decline
          </button>
        </div>
      ))}
      {ready.map((intent) => (
        <div className="order-card" key={intent.intent.intent_id}>
          <strong>Approved: {intent.intent.offer_snapshot.item_id}</strong>
          <p>{money(offerTotal(intent.intent.offer_snapshot), intent.intent.offer_snapshot.currency)}</p>
          <button onClick={() => onPlace(intent.intent.intent_id)}>
            Place order
          </button>
        </div>
      ))}
    </div>
  );
}

// The order's escrow story, told plainly on the card.
function escrowChip(order: CommerceOrder["order"], state: string): string | null {
  if (!order.escrow_held) return null;
  if (state === "delivered" && !order.charge_ref) return "evidence missing";
  if (state === "delivered" && order.charge_ref) return "held in escrow";
  if (state === "completed") return "escrow released";
  return null;
}

export function OrdersPane({
  orders,
  act,
}: {
  orders: CommerceOrder[];
  act: (work: () => Promise<string>) => void;
}) {
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [bookFor, setBookFor] = useState<string | null>(null);
  const [book, setBook] = useState<CommerceLedgerTxn[]>([]);
  const [invoices, setInvoices] = useState<Record<string, CommerceInvoice>>({});

  const openBook = useCallback(async (orderId: string) => {
    setBook((await api.commerceOrderLedger(orderId)).items ?? []);
    setBookFor(orderId);
  }, []);

  const step = useCallback(
    (
      orderId: string,
      which: "ship" | "deliver" | "accept" | "cancel" | "refund",
      body?: Record<string, string>,
    ) =>
      act(async () => {
        const after = await api.commerceOrderStep(orderId, which, body);
        return `${which}: order is now ${after.state}`;
      }),
    [act],
  );

  if (!orders.length) {
    return <p className="empty">No orders yet.</p>;
  }
  return (
    <div className="market-grid">
      {orders.map(({ order, state }) => {
        const escrow = escrowChip(order, state);
        const evidence = inputs[order.order_id] ?? "";
        return (
          <div className="order-card" key={order.order_id}>
            <strong>
              {order.offer.quantity} × {order.offer.item_id}
            </strong>{" "}
            <span className="chip">{state}</span>
            {escrow && <span className="chip">{escrow}</span>}
            <p>
              {money(offerTotal(order.offer), order.offer.currency)}
              <span className="muted"> · {order.seller_id}</span>
            </p>
            {order.tracking && <p className="muted">tracking {order.tracking}</p>}
            {state === "confirmed" && (
              <>
                <button onClick={() => step(order.order_id, "ship")}>
                  Mark shipped
                </button>{" "}
                <button
                  className="linklike"
                  onClick={() => step(order.order_id, "cancel")}
                >
                  Cancel
                </button>
              </>
            )}
            {state === "fulfilling" && (
              <>
                <label className="field">
                  Delivery evidence
                  <input
                    value={evidence}
                    onChange={(e) =>
                      setInputs((held) => ({
                        ...held,
                        [order.order_id]: e.target.value,
                      }))
                    }
                  />
                </label>
                <button
                  onClick={() =>
                    step(order.order_id, "deliver", { evidence })
                  }
                >
                  Mark delivered
                </button>
              </>
            )}
            {state === "delivered" && order.escrow_held && !order.charge_ref && (
              <>
                <p className="error">
                  Escrow stays held: nobody can verify this delivery yet.
                </p>
                <label className="field">
                  Delivery evidence
                  <input
                    value={evidence}
                    onChange={(e) =>
                      setInputs((held) => ({
                        ...held,
                        [order.order_id]: e.target.value,
                      }))
                    }
                  />
                </label>
                <button
                  onClick={() =>
                    act(async () => {
                      await api.commerceOrderEvidence(order.order_id, evidence);
                      return "evidence attached — escrow captured";
                    })
                  }
                >
                  Attach evidence
                </button>
              </>
            )}
            {state === "delivered" && !(order.escrow_held && !order.charge_ref) && (
              <button onClick={() => step(order.order_id, "accept")}>
                {order.escrow_held
                  ? "Accept — release escrow"
                  : "Accept — capture payment"}
              </button>
            )}
            {state === "completed" && (
              <>
                <button onClick={() => step(order.order_id, "refund")}>
                  Refund
                </button>{" "}
                <button
                  className="linklike"
                  onClick={() =>
                    act(async () => {
                      const invoice = await api.commerceOrderInvoice(
                        order.order_id,
                      );
                      setInvoices((held) => ({
                        ...held,
                        [order.order_id]: invoice,
                      }));
                      return `invoice ${invoice.number}`;
                    })
                  }
                >
                  Invoice
                </button>
              </>
            )}
            {invoices[order.order_id] && (
              <p className="muted">
                {invoices[order.order_id].number} ·{" "}
                {money(
                  invoices[order.order_id].total_micros,
                  invoices[order.order_id].currency,
                )}{" "}
                (tax{" "}
                {money(
                  invoices[order.order_id].tax_micros,
                  invoices[order.order_id].currency,
                )}
                )
              </p>
            )}{" "}
            <button className="linklike" onClick={() => openBook(order.order_id)}>
              Book
            </button>
            {bookFor === order.order_id && (
              <ul className="muted">
                {book.length ? (
                  book.flatMap((txn) =>
                    txn.entries.map((entry, index) => (
                      <li key={`${txn.txn_id}:${index}`}>
                        {txn.kind}: {entry.account}{" "}
                        {money(entry.amount_micros, order.offer.currency)}
                      </li>
                    )),
                  )
                ) : (
                  <li>nothing captured yet — money moves against delivery</li>
                )}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function SellPane({ onChanged }: { onChanged: () => void }) {
  const [kyc, setKyc] = useState<SellerKycView | null>(null);
  const [listings, setListings] = useState<CommerceListing[]>([]);
  const [policy, setPolicy] = useState<CommerceSalesPolicy | null>(null);
  const [floor, setFloor] = useState("");
  const [autoAccept, setAutoAccept] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [legalName, setLegalName] = useState("");
  const [companyEmail, setCompanyEmail] = useState("");
  const [title, setTitle] = useState("");
  const [price, setPrice] = useState("");
  const [quantity, setQuantity] = useState("");

  const refresh = useCallback(async () => {
    const [status, mine, sales] = await Promise.all([
      api.sellerKyc(),
      api.commerceMyListings(),
      api.commerceSalesPolicy(),
    ]);
    setKyc(status);
    setListings(mine.items ?? []);
    setPolicy(sales);
    setFloor(String(sales.absolute_floor_micros / 1_000_000));
    setAutoAccept(String(sales.auto_accept_price_micros / 1_000_000));
  }, []);

  useEffect(() => {
    void refresh().catch(() => {});
  }, [refresh]);

  const run = useCallback(
    async (work: () => Promise<string>) => {
      setError("");
      try {
        setNotice(await work());
        await refresh();
        onChanged();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [onChanged, refresh],
  );

  const verified = kyc?.status === "verified";
  return (
    <div className="market-grid">
      <div className="order-card">
        <strong>Seller identity</strong>
        {kyc === null && <p className="muted">…</p>}
        {kyc?.status === "verified" && (
          <p>
            <span className="chip">verified</span> {kyc.legal_name}
          </p>
        )}
        {kyc?.status === "pending_review" && (
          <p>
            <span className="chip">under review</span> {kyc.legal_name}
          </p>
        )}
        {kyc?.status === "rejected" && (
          <p className="error">rejected: {kyc.decision_note || "see reviewer"}</p>
        )}
        {(kyc?.status === "not_applied" || kyc?.status === "rejected") && (
          <>
            <p className="muted">
              Only verified sellers can list. Apply as a legal entity — a
              company mailbox is required, and a human reviewer decides.
            </p>
            <label className="field">
              Legal name
              <input
                value={legalName}
                onChange={(e) => setLegalName(e.target.value)}
              />
            </label>
            <label className="field">
              Company e-mail
              <input
                value={companyEmail}
                onChange={(e) => setCompanyEmail(e.target.value)}
              />
            </label>
            <button
              onClick={() =>
                run(async () => {
                  await api.sellerKycApply({
                    legal_name: legalName,
                    company_email: companyEmail,
                  });
                  return "application filed — a reviewer will decide";
                })
              }
            >
              Apply
            </button>
          </>
        )}
      </div>
      <div className="order-card">
        <strong>Signed boundary</strong>
        <p className="muted">
          Your agent quotes and accepts only inside these lines. The
          absolute floor refuses without discretion — no model crosses it.
        </p>
        <label className="field">
          Absolute floor (USD/unit)
          <input value={floor} onChange={(e) => setFloor(e.target.value)} />
        </label>
        <label className="field">
          Auto-accept price (USD/unit)
          <input
            value={autoAccept}
            onChange={(e) => setAutoAccept(e.target.value)}
          />
        </label>
        <button
          onClick={() =>
            run(async () => {
              if (!policy) return "policy not loaded yet";
              await api.commerceSalesPolicyPut({
                ...policy,
                absolute_floor_micros: Math.round(Number(floor) * 1_000_000),
                price_floor_micros: Math.round(Number(floor) * 1_000_000),
                auto_accept_price_micros: Math.round(
                  Number(autoAccept) * 1_000_000,
                ),
              });
              return "boundary signed";
            })
          }
        >
          Sign boundary
        </button>
      </div>
      <div className="order-card">
        <strong>New listing</strong>
        <label className="field">
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label className="field">
          Price (USD)
          <input value={price} onChange={(e) => setPrice(e.target.value)} />
        </label>
        <label className="field">
          Quantity
          <input value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </label>
        <button
          onClick={() =>
            run(async () => {
              await api.commerceListingCreate({
                title,
                unit_price_micros: Math.round(Number(price) * 1_000_000),
                quantity_available: Number(quantity) || 0,
              });
              return "draft saved";
            })
          }
        >
          Save draft
        </button>
      </div>
      {listings.map((listing) => (
        <div className="order-card" key={listing.listing_id}>
          <strong>{listing.title}</strong> <span className="chip">{listing.status}</span>
          <p>
            {money(listing.unit_price_micros, listing.currency)}
            <span className="muted">
              {" "}
              · {listing.quantity_available} available · v{listing.version}
            </span>
          </p>
          {listing.status === "draft" && (
            <button
              disabled={!verified}
              title={verified ? "" : "verify your seller identity first"}
              onClick={() =>
                run(async () => {
                  await api.commerceListingPublish(listing.listing_id);
                  return "published";
                })
              }
            >
              Publish
            </button>
          )}
        </div>
      ))}
      {notice && <p className="hint">{notice}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
