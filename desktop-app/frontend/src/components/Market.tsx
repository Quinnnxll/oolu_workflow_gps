import { useCallback, useEffect, useState } from "react";
import { api, session } from "../api";
import type {
  CommerceApproval,
  CommerceDeskItem,
  CommerceIntent,
  CommerceInvoice,
  CommerceLedgerTxn,
  CommerceListing,
  CommerceMilestone,
  CommerceOffer,
  CommerceOrder,
  CommerceQuote,
  CommerceRecurring,
  CommerceRfq,
  CommerceSalesPolicy,
  CommerceSourced,
  SellerKycView,
} from "../api";
import { pickLocalFiles, saveToDevice } from "../device";
import { useT } from "../ui";

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

// One product attachment, fetched with the bearer token and shown by its
// true type — the press media strip's discipline, on the shelf: a photo
// inline, a clip or a sound with controls, an honest named card for any
// other format, and the lossless download on every shape.
function ListingMediaItem({
  listingId,
  index,
  mediaType,
  name,
}: {
  listingId: string;
  index: number;
  mediaType: string;
  name: string;
}) {
  const tr = useT();
  const [blob, setBlob] = useState<Blob | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let held: string | null = null;
    void api.commerceListingMediaBlob(listingId, index).then((bytes) => {
      if (!bytes) return;
      held = URL.createObjectURL(bytes);
      setBlob(bytes);
      setUrl(held);
    });
    return () => {
      if (held) URL.revokeObjectURL(held);
    };
  }, [listingId, index]);
  if (!url || !blob) return null;
  const download = (
    <button
      type="button"
      className="linklike attachment-download"
      onClick={() => saveToDevice(name, blob)}
    >
      {tr("file.download")}
    </button>
  );
  if (mediaType.startsWith("image/")) {
    return (
      <figure className="attachment">
        <img className="press-media" src={url} alt={name} />
        <figcaption>{download}</figcaption>
      </figure>
    );
  }
  if (mediaType.startsWith("video/")) {
    return (
      <figure className="attachment">
        <video className="press-media" src={url} controls title={name} />
        <figcaption>{download}</figcaption>
      </figure>
    );
  }
  if (mediaType.startsWith("audio/")) {
    return (
      <figure className="attachment">
        <audio className="press-media" src={url} controls title={name} />
        <figcaption>{download}</figcaption>
      </figure>
    );
  }
  return (
    <span className="attachment attachment-card">
      📎 {name} {download}
    </span>
  );
}

function ListingMediaStrip({ listing }: { listing: CommerceListing }) {
  const media = listing.media ?? [];
  if (media.length === 0) return null;
  return (
    <div className="press-media-strip">
      {media.map((m, index) => (
        <ListingMediaItem
          key={`${listing.listing_id}:${index}`}
          listingId={listing.listing_id}
          index={index}
          mediaType={m.media_type}
          name={m.name}
        />
      ))}
    </div>
  );
}

type Tab = "shop" | "requests" | "approvals" | "orders" | "sell";

// The market's shared hands — one refresh, one act, one buy path —
// used by BOTH surfaces: the full-screen Market and the MarketPanel
// living in the Market agent's thread.
function useMarketDesk() {
  const [catalog, setCatalog] = useState<CommerceListing[]>([]);
  const [approvals, setApprovals] = useState<CommerceApproval[]>([]);
  const [ready, setReady] = useState<CommerceIntent[]>([]);
  const [orders, setOrders] = useState<CommerceOrder[]>([]);
  const [obligations, setObligations] = useState<CommerceRecurring[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [cat, inbox, approved, mine, standing] = await Promise.all([
        api.commerceCatalog(),
        api.commerceApprovals(),
        api.commerceIntents("approved"),
        api.commerceOrders(),
        api.commerceRecurring(),
      ]);
      setCatalog(cat.items ?? []);
      setApprovals(inbox.items ?? []);
      setReady(approved.items ?? []);
      setOrders(mine.items ?? []);
      setObligations(standing.items ?? []);
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

  return {
    catalog,
    approvals,
    ready,
    orders,
    obligations,
    notice,
    error,
    refresh,
    act,
    buyOffer,
    buy,
  };
}

export function Market() {
  const [tab, setTab] = useState<Tab>("shop");
  const {
    catalog,
    approvals,
    ready,
    orders,
    obligations,
    notice,
    error,
    refresh,
    act,
    buyOffer,
    buy,
  } = useMarketDesk();

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
      {tab === "shop" && (
        <Shop
          catalog={catalog}
          onBuy={buy}
          onBuyOffer={(offer, category) => act(() => buyOffer(offer, category))}
        />
      )}
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
      {tab === "orders" && (
        <OrdersPane orders={orders} obligations={obligations} act={act} />
      )}
      {tab === "sell" && <SellPane onChanged={refresh} />}
    </div>
  );
}

function Shop({
  catalog,
  onBuy,
  onBuyOffer,
}: {
  catalog: CommerceListing[];
  onBuy: (listing: CommerceListing) => void;
  onBuyOffer: (offer: CommerceOffer, category: string) => void;
}) {
  const [sourceCategory, setSourceCategory] = useState("");
  const [sourced, setSourced] = useState<CommerceSourced[] | null>(null);
  return (
    <div className="market-grid">
      <div className="order-card">
        <strong>Source everywhere</strong>
        <p className="muted">
          One search across this market and every federated peer — one
          normalized comparison, substitutes named for what they miss.
        </p>
        <label className="field">
          Category
          <input
            value={sourceCategory}
            onChange={(e) => setSourceCategory(e.target.value)}
          />
        </label>
        <button
          onClick={() =>
            void api
              .commerceSource(sourceCategory, 1)
              .then(({ items }) => setSourced(items ?? []))
              .catch(() => setSourced([]))
          }
        >
          Search
        </button>
        {sourced !== null && !sourced.length && (
          <p className="muted">No shelf can serve this.</p>
        )}
      </div>
      {(sourced ?? []).map((row) => (
        <div className="order-card" key={`${row.origin}:${row.offer.offer_id}`}>
          <strong>{row.offer.item_id}</strong>{" "}
          <span className="chip">
            {row.origin.startsWith("peer:")
              ? row.origin.slice(5)
              : "this market"}
          </span>
          {!row.eligible && <span className="chip">substitute</span>}
          <p>{money(offerTotal(row.offer), row.offer.currency)}</p>
          {row.eligible ? (
            <button onClick={() => onBuyOffer(row.offer, sourceCategory)}>
              Buy
            </button>
          ) : (
            <ul className="muted">
              {row.gaps.map((gap) => (
                <li key={gap}>{gap}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
      {!catalog.length && sourced === null && (
        <p className="empty">Nothing is listed yet.</p>
      )}
      {catalog.map((listing) => (
        <div className="order-card" key={listing.listing_id}>
          <strong>{listing.title}</strong>
          {listing.description && <p className="muted">{listing.description}</p>}
          {/* The product's own media — photos, clips, sound — by
              reference through the listing media door. */}
          <ListingMediaStrip listing={listing} />
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

// One recurring obligation card: the approved terms, renewed or ended.
function Obligations({
  obligations,
  act,
}: {
  obligations: CommerceRecurring[];
  act: (work: () => Promise<string>) => void;
}) {
  if (!obligations.length) return null;
  return (
    <>
      {obligations.map((obligation) => (
        <div className="order-card" key={obligation.obligation_id}>
          <strong>Standing: {obligation.offer.item_id}</strong>{" "}
          <span className="chip">{obligation.state}</span>
          <p>
            {money(offerTotal(obligation.offer), obligation.offer.currency)}
            <span className="muted">
              {" "}
              · every {obligation.period_days} days · renewed{" "}
              {obligation.renewals}×
            </span>
          </p>
          {obligation.state === "active" && (
            <>
              <button
                onClick={() =>
                  act(async () => {
                    // Identical terms renew without a human; the intent
                    // places like any order — digest law included.
                    const renewal = await api.commerceRecurringRenew(
                      obligation.obligation_id,
                    );
                    const order = await api.commerceOrderPlace(
                      renewal.intent.intent_id,
                    );
                    return `renewed — order ${order.state}`;
                  })
                }
              >
                Renew now
              </button>{" "}
              <button
                className="linklike"
                onClick={() =>
                  act(async () => {
                    await api.commerceRecurringCancel(
                      obligation.obligation_id,
                    );
                    return "obligation cancelled — nothing renews after";
                  })
                }
              >
                Cancel
              </button>
            </>
          )}
        </div>
      ))}
    </>
  );
}

// A milestone order's schedule, one tranche at a time.
function MilestoneRows({
  order,
  milestones,
  act,
  onChanged,
}: {
  order: CommerceOrder["order"];
  milestones: CommerceMilestone[];
  act: (work: () => Promise<string>) => void;
  onChanged: () => void;
}) {
  const [evidence, setEvidence] = useState("");
  return (
    <ul className="muted">
      {milestones.map((milestone) => (
        <li key={milestone.index}>
          {milestone.title}:{" "}
          {money(milestone.amount_micros, order.offer.currency)}{" "}
          <span className="chip">{milestone.state}</span>{" "}
          {milestone.state === "pending" && (
            <>
              <input
                placeholder="evidence"
                value={evidence}
                onChange={(e) => setEvidence(e.target.value)}
              />{" "}
              <button
                onClick={() =>
                  act(async () => {
                    await api.commerceMilestoneStep(
                      order.order_id,
                      milestone.index,
                      "deliver",
                      { evidence },
                    );
                    onChanged();
                    return `milestone ${milestone.index} delivered`;
                  })
                }
              >
                Deliver
              </button>
            </>
          )}
          {milestone.state === "delivered" && (
            <>
              <button
                onClick={() =>
                  act(async () => {
                    await api.commerceMilestoneStep(
                      order.order_id,
                      milestone.index,
                      "accept",
                    );
                    onChanged();
                    return `tranche ${milestone.index} released from escrow`;
                  })
                }
              >
                Accept tranche
              </button>{" "}
              <button
                className="linklike"
                onClick={() =>
                  act(async () => {
                    await api.commerceMilestoneStep(
                      order.order_id,
                      milestone.index,
                      "fail",
                      { reason: "does not match the specification" },
                    );
                    onChanged();
                    return "milestone failed — the remainder is frozen in escrow";
                  })
                }
              >
                Fail
              </button>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

export function OrdersPane({
  orders,
  obligations = [],
  act,
}: {
  orders: CommerceOrder[];
  obligations?: CommerceRecurring[];
  act: (work: () => Promise<string>) => void;
}) {
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [bookFor, setBookFor] = useState<string | null>(null);
  const [book, setBook] = useState<CommerceLedgerTxn[]>([]);
  const [invoices, setInvoices] = useState<Record<string, CommerceInvoice>>({});
  const [schedules, setSchedules] = useState<
    Record<string, CommerceMilestone[]>
  >({});

  const loadMilestones = useCallback(async (orderId: string) => {
    const { items } = await api.commerceOrderMilestones(orderId);
    setSchedules((held) => ({ ...held, [orderId]: items ?? [] }));
  }, []);

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

  if (!orders.length && !obligations.length) {
    return <p className="empty">No orders yet.</p>;
  }
  return (
    <div className="market-grid">
      <Obligations obligations={obligations} act={act} />
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
            )}
            {(order.offer.milestones?.length ?? 0) > 0 && (
              <>
                {" "}
                <button
                  className="linklike"
                  onClick={() => void loadMilestones(order.order_id)}
                >
                  Milestones
                </button>
                {schedules[order.order_id] && (
                  <MilestoneRows
                    order={order}
                    milestones={schedules[order.order_id]}
                    act={act}
                    onChanged={() => void loadMilestones(order.order_id)}
                  />
                )}
                {state === "disputed" && (
                  <button
                    onClick={() =>
                      act(async () => {
                        await api.commerceRefundUnreleased(
                          order.order_id,
                          "frozen milestones resolved",
                        );
                        return "the unreleased remainder returned to you";
                      })
                    }
                  >
                    Refund unreleased
                  </button>
                )}
              </>
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
  // Product media waiting on the draft: uploaded from the device (the
  // blob door keeps true bytes; inline fallback), removable until saved.
  const [media, setMedia] = useState<{ id: string; name: string }[]>([]);

  async function addMedia() {
    const picked = await pickLocalFiles();
    for (const file of picked) {
      try {
        // Blob door first — TRUE bytes, so the listing's media downloads
        // losslessly; a blob-less host falls back inline (saveToDrawer).
        const saved = await api.saveToDrawer(file);
        setMedia((m) =>
          m.some((f) => f.id === saved.file_id) || m.length >= 6
            ? m
            : [...m, { id: saved.file_id, name: saved.name }],
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  }

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
        {/* Multimedia on the product: photos, clips, or sound showing
            the real thing — uploaded from the device, riding the draft
            as drawer refs (the press attachment law, on the shelf). */}
        <button type="button" className="ghost" onClick={() => void addMedia()}>
          Add photo, video, or audio
        </button>
        {media.length > 0 && (
          <p className="muted">
            {media.map((f) => (
              <button
                key={f.id}
                type="button"
                className="chip"
                title="Remove attachment"
                onClick={() =>
                  setMedia((m) => m.filter((x) => x.id !== f.id))
                }
              >
                {f.name} ✕
              </button>
            ))}
          </p>
        )}
        <button
          onClick={() =>
            run(async () => {
              await api.commerceListingCreate({
                title,
                unit_price_micros: Math.round(Number(price) * 1_000_000),
                quantity_available: Number(quantity) || 0,
                ...(media.length > 0
                  ? { file_ids: media.map((f) => f.id) }
                  : {}),
              });
              setMedia([]);
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
          <ListingMediaStrip listing={listing} />
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

// The Market agent's thread surface: every marketplace function block,
// as form blocks IN the conversation — the way the press shelf heads
// the News thread. The brief comes first (where the member's position
// meets the market's demand — the same items the pulse pushes), then
// the five doors, then the list-out of everything they created here.
export function MarketPanel() {
  const {
    catalog,
    approvals,
    ready,
    orders,
    obligations,
    notice,
    error,
    refresh,
    act,
    buyOffer,
    buy,
  } = useMarketDesk();
  const [desk, setDesk] = useState<CommerceDeskItem[] | null>(null);
  const [briefOn, setBriefOn] = useState(false);
  const [mine, setMine] = useState<{
    listings: CommerceListing[];
    requests: CommerceRfq[];
    orders: CommerceOrder[];
  } | null>(null);

  const refreshDesk = useCallback(() => {
    void api
      .commerceDesk()
      .then(({ items, brief_schedule }) => {
        setDesk(items ?? []);
        setBriefOn(brief_schedule !== null && brief_schedule !== undefined);
      })
      .catch(() => setDesk(null)); // a pre-desk host: no brief block
    void api
      .commerceMine()
      .then((created) => setMine(created))
      .catch(() => setMine(null));
  }, []);

  useEffect(() => {
    refreshDesk();
  }, [refreshDesk]);

  return (
    <div className="market market-panel">
      {/* The brief: pushed to this thread on the standing schedule, and
          shown live here — counts and names, never invented urgency. */}
      {desk !== null && (
        <div className="order-card">
          <strong>Where you meet the market</strong>
          <button
            className={`ghost${briefOn ? " on" : ""}`}
            title="Push this brief to this thread every day."
            onClick={() =>
              void api
                .commerceDeskSchedule({ enabled: !briefOn })
                .then(({ brief_schedule }) =>
                  setBriefOn(brief_schedule !== null),
                )
                .catch(() => {})
            }
          >
            {briefOn ? "Daily brief ✓" : "Daily brief"}
          </button>
          {desk.length === 0 && (
            <p className="muted">
              Nothing waits on you — no approvals, no orders needing
              action, no open request matching what you sell.
            </p>
          )}
          <ul>
            {desk.map((item, i) => (
              <li key={`${item.kind}:${item.ref}:${i}`}>{item.text}</li>
            ))}
          </ul>
        </div>
      )}
      {notice && <p className="hint">{notice}</p>}
      {error && <p className="error">{error}</p>}
      <details className="market-block" open>
        <summary>
          Approvals
          {approvals.length + ready.length ? (
            <span className="badge">{approvals.length + ready.length}</span>
          ) : null}
        </summary>
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
      </details>
      <details className="market-block">
        <summary>Shop</summary>
        <Shop
          catalog={catalog}
          onBuy={buy}
          onBuyOffer={(offer, category) => act(() => buyOffer(offer, category))}
        />
      </details>
      <details className="market-block">
        <summary>Requests</summary>
        <RequestsPane
          onAwardBuy={(offer, category) => act(() => buyOffer(offer, category))}
        />
      </details>
      <details className="market-block">
        <summary>
          Orders
          {orders.length ? <span className="badge">{orders.length}</span> : null}
        </summary>
        <OrdersPane orders={orders} obligations={obligations} act={act} />
      </details>
      <details className="market-block">
        <summary>Sell</summary>
        <SellPane
          onChanged={() => {
            void refresh();
            refreshDesk();
          }}
        />
      </details>
      {/* The list-out: everything the member created on the platform,
          grouped and named — never invisible. */}
      {mine !== null && (
        <details className="market-block">
          <summary>Created here</summary>
          <div className="market-grid">
            <div className="order-card">
              <strong>Listings ({mine.listings.length})</strong>
              <ul>
                {mine.listings.map((x) => (
                  <li key={x.listing_id}>
                    “{x.title}” — {x.status}
                    {(x.media ?? []).length > 0
                      ? ` · ${(x.media ?? []).length} media`
                      : ""}
                  </li>
                ))}
              </ul>
            </div>
            <div className="order-card">
              <strong>Requests ({mine.requests.length})</strong>
              <ul>
                {mine.requests.map((r) => (
                  <li key={r.rfq_id}>
                    {r.specification.category} ×{r.specification.quantity} —{" "}
                    {r.state}
                  </li>
                ))}
              </ul>
            </div>
            <div className="order-card">
              <strong>Orders ({mine.orders.length})</strong>
              <ul>
                {mine.orders.map((o) => (
                  <li key={o.order.order_id}>
                    {o.order.order_id.slice(0, 8)} — {o.state}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </details>
      )}
    </div>
  );
}
