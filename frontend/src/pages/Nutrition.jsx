import { useEffect, useState } from "react";
import { auth, db } from "../services/firebase";
import { collection, doc, orderBy, query, limit, onSnapshot } from "firebase/firestore";
import { postJSON } from "../services/api";
import TopNav from "../components/TopNav";

export default function Nutrition() {
  const [plan, setPlan] = useState(null);
  const [planMeta, setPlanMeta] = useState(null);
  const [progressSummary, setProgressSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [availableInput, setAvailableInput] = useState("");
  const [unavailableInput, setUnavailableInput] = useState("");
  const [providerSelection, setProviderSelection] = useState({
    blinkit: true,
    zepto: true,
    swiggy_instamart: true,
    amazon: false,
    bigbasket: false,
  });
  const [shoppingLoading, setShoppingLoading] = useState(false);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [shoppingPlan, setShoppingPlan] = useState(null);
  const [unavailableItems, setUnavailableItems] = useState([]);

  const uid = auth.currentUser?.uid || null;

  useEffect(() => {
    const user = auth.currentUser;

    let cached = null;
    try {
      cached = JSON.parse(localStorage.getItem("nutritionPlan") || "null");
    } catch {}

    if (!user) {
      setPlan(cached);
      setPlanMeta(null);
      setLoading(false);
      return;
    }

    const q = query(
      collection(db, "users", user.uid, "nutritionPlans"),
      orderBy("createdAt", "desc"),
      limit(1)
    );

    const progressRef = doc(db, "users", user.uid, "progressStats", "summary");
    const unsubProgress = onSnapshot(
      progressRef,
      (snap) => {
        if (!snap.exists()) {
          setProgressSummary(null);
          return;
        }
        setProgressSummary(snap.data() || null);
      },
      (e) => {
        console.error(e);
        setProgressSummary(null);
      }
    );

    const unsub = onSnapshot(
      q,
      (snap) => {
        if (!snap.empty) {
          const latestDoc = snap.docs[0];
          const latestData = latestDoc.data();
          const latestPlan = latestData.plan || [];
          setPlan(latestPlan);
          const createdAt = latestData.createdAt?.toDate ? latestData.createdAt.toDate() : null;
          setPlanMeta({
            id: latestDoc.id,
            createdAt,
            evolvedFromActivity: Boolean(latestData.evolvedFromActivity),
            evolutionBanner: String(latestData.evolutionBanner || "").trim(),
            evolutionMode: String(latestData?.evolutionMeta?.mode || "").trim(),
          });
          try {
            localStorage.setItem("nutritionPlan", JSON.stringify(latestPlan));
          } catch {}
        } else {
          setPlan(cached);
          setPlanMeta(null);
        }
        setLoading(false);
      },
      (e) => {
        console.error(e);
        setPlan(cached);
        setPlanMeta(null);
        setLoading(false);
      }
    );

    return () => {
      unsub();
      unsubProgress();
    };
  }, []);

  useEffect(() => {
    if (!uid) {
      setUnavailableItems([]);
      return () => {};
    }

    const ref = doc(db, "users", uid, "pantry", "current");

    const unsubscribe = onSnapshot(ref, (docSnap) => {
      if (docSnap.exists()) {
        const data = docSnap.data() || {};

        const unavailable = Object.keys(data)
          .filter((item) => data[item] === false)
          .map((item) => item.toLowerCase().trim())
          .filter(Boolean)
          .sort();

        setUnavailableItems(unavailable);
        setUnavailableInput(unavailable.join(", "));
      } else {
        setUnavailableItems([]);
        setUnavailableInput("");
      }
    });

    return () => unsubscribe();
  }, [uid]);

  const totalWorkoutDays = Number(progressSummary?.total_workout_days || 0);
  const completedDaysInCurrentCycle = totalWorkoutDays % 7;
  const visiblePlanDays = Array.isArray(plan) ? plan.slice(completedDaysInCurrentCycle) : [];

  if (loading) return <div className="text-white p-8">Loading...</div>;
  if (!plan) return <div className="text-white p-8">No nutrition plan yet</div>;

  const parseItems = (raw) =>
    (raw || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);

  const selectedProviders = Object.entries(providerSelection)
    .filter(([, enabled]) => enabled)
    .map(([provider]) => provider);

  const runShoppingAgent = async () => {
    const user = auth.currentUser;
    if (!user) {
      alert("Please login first");
      return;
    }

    setShoppingLoading(true);
    try {
      const data = await postJSON("http://127.0.0.1:8000/nutrition/shopping/plan", {
        user_id: user.uid,
        available_items: parseItems(availableInput),
        unavailable_items: parseItems(unavailableInput),
        preferred_providers: selectedProviders,
      });
      setShoppingPlan(data || null);
    } catch (e) {
      alert(e?.message || "Failed to run shopping agent");
    } finally {
      setShoppingLoading(false);
    }
  };

  const confirmShoppingOrder = async (provider) => {
    const user = auth.currentUser;
    if (!user || !shoppingPlan?.shopping_plan_id) return;

    setConfirmLoading(true);
    try {
      const data = await postJSON("http://127.0.0.1:8000/nutrition/shopping/confirm", {
        user_id: user.uid,
        shopping_plan_id: shoppingPlan.shopping_plan_id,
        provider,
        action: "place_order",
      });

      setShoppingPlan((prev) => ({
        ...(prev || {}),
        confirmation: data,
      }));
    } catch (e) {
      alert(e?.message || "Failed to confirm order");
    } finally {
      setConfirmLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-black text-white p-6">
      <TopNav />

      <h1 className="text-2xl font-bold mb-6">Your Nutrition Plan 🥗</h1>

      {planMeta?.id && (
        <div className="mb-6 inline-flex items-center gap-2 rounded-md border border-white/15 bg-white/5 px-3 py-1 text-xs text-white/80">
          <span className="font-semibold">Plan Version</span>
          <span>#{planMeta.id.slice(0, 8)}</span>
          <span>•</span>
          <span>
            {planMeta.createdAt
              ? `Updated ${planMeta.createdAt.toLocaleString()}`
              : "Updated just now"}
          </span>
        </div>
      )}

      {planMeta?.evolvedFromActivity && (
        <div className="mb-4 rounded-lg border border-emerald-400/40 bg-emerald-900/20 px-4 py-3 text-sm text-emerald-100">
          <div className="font-semibold">{planMeta?.evolutionBanner || "Your plan evolved based on your activity"}</div>
          {planMeta?.evolutionMode && (
            <div className="mt-1 text-xs text-emerald-200/90">Mode: {planMeta.evolutionMode.replaceAll("_", " ")}</div>
          )}
        </div>
      )}

      {totalWorkoutDays > 0 && (
        <div className="mb-4 text-xs text-green-300/90">
          Completed in current cycle: {completedDaysInCurrentCycle} day(s)
        </div>
      )}

      <div className="mb-6 rounded-xl border border-white/15 bg-white/5 p-4 space-y-4">
        <div>
          <h2 className="text-lg font-semibold">Nutrition Shopping Agent</h2>
          <p className="text-sm text-gray-300 mt-1">
            Track unavailable ingredients, prepare provider carts, and confirm before placing orders.
          </p>
        </div>

        <div className="grid md:grid-cols-2 gap-3">
          <div>
            <label className="text-xs text-gray-300">Available items (comma separated)</label>
            <textarea
              value={availableInput}
              onChange={(e) => setAvailableInput(e.target.value)}
              placeholder="eggs, oats, banana"
              className="mt-1 w-full min-h-[88px] rounded-lg bg-zinc-900 border border-zinc-700 p-2 text-sm"
            />
          </div>
          <div>
            <label className="text-xs text-gray-300">Unavailable items (comma separated)</label>
            <textarea
              value={unavailableInput}
              onChange={(e) => setUnavailableInput(e.target.value)}
              placeholder="curd, paneer, roasted chana"
              className="mt-1 w-full min-h-[88px] rounded-lg bg-zinc-900 border border-zinc-700 p-2 text-sm"
            />
          </div>
        </div>

        <div>
          <div className="text-xs text-gray-300 mb-2">Preferred providers</div>
          <div className="flex flex-wrap gap-3">
            {[
              ["blinkit", "Blinkit"],
              ["zepto", "Zepto"],
              ["swiggy_instamart", "Swiggy Instamart"],
              ["amazon", "Amazon"],
              ["bigbasket", "BigBasket"],
            ].map(([key, label]) => (
              <label key={key} className="text-sm flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={Boolean(providerSelection[key])}
                  onChange={(e) =>
                    setProviderSelection((prev) => ({
                      ...prev,
                      [key]: e.target.checked,
                    }))
                  }
                />
                {label}
              </label>
            ))}
          </div>
        </div>

        <button
          onClick={runShoppingAgent}
          disabled={shoppingLoading}
          className="px-4 py-2 rounded-lg bg-green-500 text-black font-semibold disabled:opacity-60"
        >
          {shoppingLoading ? "Preparing carts..." : "Run Shopping Agent"}
        </button>

        {shoppingPlan?.message && (
          <div className="text-sm text-amber-300">{shoppingPlan.message}</div>
        )}

        {Array.isArray(shoppingPlan?.cart_items) && shoppingPlan.cart_items.length > 0 && (
          <div className="space-y-3">
            <div className="text-sm font-semibold">Items to purchase</div>
            <div className="grid md:grid-cols-2 gap-2">
              {shoppingPlan.cart_items.map((item, idx) => (
                <div key={`${item.item}-${idx}`} className="rounded-md border border-zinc-700 bg-zinc-900/70 p-2 text-sm">
                  <div className="font-medium">{item.item}</div>
                  <div className="text-xs text-zinc-400">Qty hint: {item.quantity_hint}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {Array.isArray(shoppingPlan?.provider_plans) && shoppingPlan.provider_plans.length > 0 && (
          <div className="space-y-3">
            <div className="text-sm font-semibold">Provider carts (confirmation required)</div>
            <div className="space-y-3">
              {shoppingPlan.provider_plans.map((providerPlan, idx) => (
                <div key={`${providerPlan.provider}-${idx}`} className="rounded-lg border border-zinc-700 bg-zinc-900/70 p-3 space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-medium">{providerPlan.provider}</div>
                    <a
                      href={providerPlan.cart_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs text-green-300 underline"
                    >
                      Open cart search
                    </a>
                  </div>

                  <div className="text-xs text-zinc-400">{providerPlan.note}</div>

                  <div className="flex flex-wrap gap-2">
                    {(providerPlan.item_links || []).slice(0, 6).map((itemLink, itemIdx) => (
                      <a
                        key={`${itemLink.item}-${itemIdx}`}
                        href={itemLink.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs px-2 py-1 rounded-md border border-zinc-700 bg-zinc-800 hover:bg-zinc-700"
                      >
                        {itemLink.item}
                      </a>
                    ))}
                  </div>

                  <button
                    onClick={() => confirmShoppingOrder(providerPlan.provider)}
                    disabled={confirmLoading}
                    className="px-3 py-1.5 rounded-md bg-emerald-500 text-black text-sm font-semibold disabled:opacity-60"
                  >
                    {confirmLoading ? "Confirming..." : `Confirm Order with ${providerPlan.provider}`}
                  </button>
                </div>
              ))}
            </div>

            {shoppingPlan?.confirmation?.status && (
              <div className="text-sm text-green-300">
                Order action recorded: {shoppingPlan.confirmation.status} ({shoppingPlan.confirmation.provider})
              </div>
            )}
          </div>
        )}
      </div>

      <div className="grid md:grid-cols-2 gap-6">
        {visiblePlanDays.length > 0 ? (
          visiblePlanDays.map((day, i) => (
            <div key={`${day?.day || "day"}-${i}`} className="border border-white/10 rounded-xl p-5">
              <h2 className="font-bold">{day.day}</h2>

              <Section title="Breakfast" items={day.breakfast} />
              <Section title="Lunch" items={day.lunch} />
              <Section title="Snacks" items={day.snacks} />
              <Section title="Dinner" items={day.dinner} />

              <p className="text-green-300 text-sm mt-3">💡 {day.tip}</p>
            </div>
          ))
        ) : (
          <div className="md:col-span-2 border border-white/10 rounded-xl p-5 bg-white/5">
            <h2 className="font-bold">No pending nutrition days in this cycle</h2>
            <p className="text-sm text-gray-300 mt-2">
              Great consistency. Ask Coach to refresh your next cycle nutrition plan.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function Section({ title, items }) {
  const safeItems = Array.isArray(items) ? items : [];

  return (
    <div className="mt-3">
      <div className="text-sm font-semibold">{title}</div>
      <ul className="text-sm text-gray-300 list-disc ml-5">
        {safeItems.map((i, idx) => (
          <li key={idx}>{i}</li>
        ))}
      </ul>
    </div>
  );
}
