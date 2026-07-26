import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type {
  CommerceApproval,
  CommerceIntent,
  CommerceLedgerTxn,
  CommerceListing,
  CommerceOrder,
  SellerKycView,
} from "../api";

// The market surface (marketplace-build-plan M1): buying walks the spine's
// law — offer → intent → verdict → (approval) → order — and this screen
// never shortcuts it: every button is one of the gateway's doors, and what
// the approval card shows is the server's own digest-rendered summary, not
// a client paraphrase.

export function money(micros: number, currency: string): string {
  return `${(micros / 1_000_000).toFixed(2)} ${currency}`;
}

// The fulfillment step each order state offers next. Capture happens at
// acceptance — the accept button says so, because that is the moment money
// becomes irreversible.
const STEP_ACTIONS: Record<
  string,
  { step: "ship" | "deliver" | "accept" | "cancel" | "refund"; label: string }[]
> = {
  confirmed: [
    { step: "ship", label: "Mark shipped" },
    { step: "cancel", label: "Cancel" },
  ],
  fulfilling: [{ step: "deliver", label: "Mark delivered" }],
  delivered: [{ step: "accept", label: "Accept — capture payment" }],
  completed: [{ step: "refund", label: "Refund" }],
};

type Tab = "shop" | "approvals" | "orders" | "sell";

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

  const buy = useCallback(
    (listing: CommerceListing) =>
      act(async () => {
        const offer = await api.commerceListingOffer(listing.listing_id, 1);
        const intent = await api.commerceIntentCreate(offer, listing.category);
        if (intent.state === "denied") {
          return `refused: ${intent.verdict.reasons.join("; ")}`;
        }
        if (intent.state === "approval_pending") {
          return "sent for your approval — the exact terms are in Approvals";
        }
        await api.commerceOrderPlace(intent.intent.intent_id);
        return `order placed — ${money(
          offer.subtotal_micros + offer.tax_estimate_micros + offer.fees_micros,
          offer.currency,
        )} authorized, captured only when you accept delivery`;
      }),
    [act],
  );

  return (
    <div className="market">
      <nav className="dev-nav">
        <button className={tab === "shop" ? "on" : ""} onClick={() => setTab("shop")}>
          Shop
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
      {tab === "approvals" && (
        <ApprovalsPane
          approvals={approvals}
          ready={ready}
          onDecide={(id, approve) =>
            act(async () => {
              const decided = await api.commerceDecide(id, approve);
              return approve
                ? `approved — state ${decided.state}`
                : "declined";
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
        <OrdersPane
          orders={orders}
          onStep={(orderId, step) =>
            act(async () => {
              const after = await api.commerceOrderStep(orderId, step);
              return `${step}: order is now ${after.state}`;
            })
          }
        />
      )}
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
          <p>
            {money(
              intent.intent.offer_snapshot.subtotal_micros +
                intent.intent.offer_snapshot.tax_estimate_micros +
                intent.intent.offer_snapshot.fees_micros,
              intent.intent.offer_snapshot.currency,
            )}
          </p>
          <button onClick={() => onPlace(intent.intent.intent_id)}>
            Place order
          </button>
        </div>
      ))}
    </div>
  );
}

function OrdersPane({
  orders,
  onStep,
}: {
  orders: CommerceOrder[];
  onStep: (
    orderId: string,
    step: "ship" | "deliver" | "accept" | "cancel" | "refund",
  ) => void;
}) {
  const [bookFor, setBookFor] = useState<string | null>(null);
  const [book, setBook] = useState<CommerceLedgerTxn[]>([]);
  const openBook = useCallback(async (orderId: string) => {
    setBook((await api.commerceOrderLedger(orderId)).items);
    setBookFor(orderId);
  }, []);
  if (!orders.length) {
    return <p className="empty">No orders yet.</p>;
  }
  return (
    <div className="market-grid">
      {orders.map(({ order, state }) => (
        <div className="order-card" key={order.order_id}>
          <strong>
            {order.offer.quantity} × {order.offer.item_id}
          </strong>{" "}
          <span className="chip">{state}</span>
          <p>
            {money(
              order.offer.subtotal_micros +
                order.offer.tax_estimate_micros +
                order.offer.fees_micros,
              order.offer.currency,
            )}
            <span className="muted"> · {order.seller_id}</span>
          </p>
          {order.tracking && <p className="muted">tracking {order.tracking}</p>}
          {(STEP_ACTIONS[state] ?? []).map(({ step, label }) => (
            <button key={step} onClick={() => onStep(order.order_id, step)}>
              {label}
            </button>
          ))}{" "}
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
                <li>nothing captured yet — money moves on acceptance</li>
              )}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}

export function SellPane({ onChanged }: { onChanged: () => void }) {
  const [kyc, setKyc] = useState<SellerKycView | null>(null);
  const [listings, setListings] = useState<CommerceListing[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [legalName, setLegalName] = useState("");
  const [companyEmail, setCompanyEmail] = useState("");
  const [title, setTitle] = useState("");
  const [price, setPrice] = useState("");
  const [quantity, setQuantity] = useState("");

  const refresh = useCallback(async () => {
    const [status, mine] = await Promise.all([
      api.sellerKyc(),
      api.commerceMyListings(),
    ]);
    setKyc(status);
    setListings(mine.items ?? []);
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
