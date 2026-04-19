import { useEffect, useState } from "react";
import { auth, db } from "../services/firebase";
import { collection, doc, orderBy, query, limit, onSnapshot, updateDoc } from "firebase/firestore";
import { postJSON } from "../services/api";
import TopNav from "../components/TopNav";

export default function Nutrition() {
  const [plan, setPlan] = useState(null);
  const [planMeta, setPlanMeta] = useState(null);
  const [progressSummary, setProgressSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [availableInput, setAvailableInput] = useState("");
  const [unavailableInput, setUnavailableInput] = useState("");
  const [cityInput, setCityInput] = useState("hyderabad");
  const [budgetMessageInput, setBudgetMessageInput] = useState("");
  const [providerSelection, setProviderSelection] = useState({
    blinkit: true,
    zepto: true,
    amazon: false,
    bigbasket: false,
  });
  const [shoppingLoading, setShoppingLoading] = useState(false);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [shoppingPlan, setShoppingPlan] = useState(null);
  const [unavailableItems, setUnavailableItems] = useState([]);
  const [guidedMode, setGuidedMode] = useState(false);
  const [addedItems, setAddedItems] = useState([]);
  const [proactiveSuggestion, setProactiveSuggestion] = useState(null);
  const [followupMessage, setFollowupMessage] = useState("");
  const [outcomeMessage, setOutcomeMessage] = useState("");

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

  useEffect(() => {
    if (!uid) {
      setProactiveSuggestion(null);
      return;
    }

    let active = true;
    (async () => {
      try {
        const data = await postJSON("http://127.0.0.1:8000/nutrition/shopping/proactive-check", {
          user_id: uid,
        });
        if (!active) return;
        setProactiveSuggestion(data || null);
      } catch {
        if (!active) return;
        setProactiveSuggestion(null);
      }
    })();

    return () => {
      active = false;
    };
  }, [uid]);

  const totalWorkoutDays = Number(progressSummary?.total_workout_days || 0);
  const completedDaysInCurrentCycle = totalWorkoutDays % 7;
  const visiblePlanDays = Array.isArray(plan) ? plan.slice(completedDaysInCurrentCycle) : [];

  const parseItems = (raw) =>
    (raw || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);

  const blockedProviderKeys = new Set(["swiggy_instamart", "instamart"]);
  const normalizeProviderKey = (value) =>
    String(value || "")
      .toLowerCase()
      .trim()
      .replaceAll(" ", "_");
  const isProviderAllowed = (providerName) => !blockedProviderKeys.has(normalizeProviderKey(providerName));

  const selectedProviders = Object.entries(providerSelection)
    .filter(([, enabled]) => enabled)
    .map(([provider]) => provider);

  const formattedEstimatedCost =
    typeof shoppingPlan?.estimated_cost === "number"
      ? `Rs ${shoppingPlan.estimated_cost.toLocaleString()}`
      : null;
  const shoppingCoverageDays =
    typeof shoppingPlan?.coverage_days === "number" ? shoppingPlan.coverage_days : null;
  const shoppingSuggestions = Array.isArray(shoppingPlan?.suggestions)
    ? shoppingPlan.suggestions.filter(Boolean)
    : [];
  const missingIngredients = Array.isArray(shoppingPlan?.missing_ingredients)
    ? shoppingPlan.missing_ingredients.filter(Boolean)
    : [];
  const bestProvider =
    shoppingPlan?.best_provider && isProviderAllowed(shoppingPlan.best_provider.name)
      ? shoppingPlan.best_provider
      : null;
  const alternatives = Array.isArray(shoppingPlan?.alternatives)
    ? shoppingPlan.alternatives.filter((x) => x && x.name && isProviderAllowed(x.name))
    : [];
  const selectionReason = String(shoppingPlan?.reason || "").trim();
  const detectedBudget =
    typeof shoppingPlan?.budget === "number" ? shoppingPlan.budget : null;
  const withinBudget =
    typeof shoppingPlan?.within_budget === "boolean" ? shoppingPlan.within_budget : null;
  const items = (shoppingPlan?.cart_items || []).map((entry) => entry?.item).filter(Boolean);
  const uniqueItems = [...new Set(items)];
  const nextItem = uniqueItems.find((item) => !addedItems.includes(item));
  const progress = uniqueItems.length
    ? Math.round((addedItems.length / uniqueItems.length) * 100)
    : 0;

  useEffect(() => {
    setGuidedMode(false);
    setAddedItems([]);
    setFollowupMessage("");
    setOutcomeMessage("");
  }, [shoppingPlan?.shopping_plan_id]);

  useEffect(() => {
    if (!guidedMode || uniqueItems.length === 0) return;
    if (addedItems.length !== uniqueItems.length) return;

    const timeoutId = setTimeout(() => setGuidedMode(false), 3000);
    return () => clearTimeout(timeoutId);
  }, [addedItems, guidedMode, uniqueItems.length]);

  useEffect(() => {
    const user = auth.currentUser;
    const shoppingPlanId = shoppingPlan?.shopping_plan_id;
    if (!user || !shoppingPlanId || uniqueItems.length === 0) return;

    const syncProgress = async () => {
      try {
        await updateDoc(doc(db, "users", user.uid, "nutritionShoppingPlans", shoppingPlanId), {
          items: uniqueItems,
          added_items: addedItems,
          status: addedItems.length < uniqueItems.length ? "in_progress" : "completed",
        });
      } catch {
        // ignore Firestore client-side sync failures and continue with backend sync
      }

      try {
        await postJSON("http://127.0.0.1:8000/nutrition/shopping/progress", {
          user_id: user.uid,
          shopping_plan_id: shoppingPlanId,
          items: uniqueItems,
          added_items: addedItems,
        });

        const followup = await postJSON("http://127.0.0.1:8000/nutrition/shopping/followup", {
          user_id: user.uid,
          shopping_plan_id: shoppingPlanId,
        });
        setFollowupMessage(String(followup?.message || "").trim());
      } catch {
        // ignore transient sync issues
      }
    };

    void syncProgress();
  }, [addedItems, db, shoppingPlan?.shopping_plan_id, uniqueItems]);

  if (loading) return <div className="text-white p-8">Loading...</div>;
  if (!plan) return <div className="text-white p-8">No nutrition plan yet</div>;

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
        city: cityInput,
        user_message: budgetMessageInput,
      });
      setShoppingPlan(data || null);
    } catch (e) {
      alert(e?.message || "Failed to run shopping agent");
    } finally {
      setShoppingLoading(false);
    }
  };

  const getProviderSearchLink = (provider, item) => {
    const p = String(provider || "").toLowerCase().trim();
    const query = encodeURIComponent(String(item || "").trim());

    switch (p) {
      case "zepto":
        return `https://www.zeptonow.com/search?q=${query}`;
      case "blinkit":
        return `https://blinkit.com/s/?q=${query}`;
      case "bigbasket":
        return `https://www.bigbasket.com/ps/?q=${query}`;
      case "amazon":
        return `https://www.amazon.in/s?k=${query}`;
      default:
        return "";
    }
  };

  const handleOpenItem = (provider, item) => {
    const url = getProviderSearchLink(provider, item);
    if (url) {
      console.log("Opening:", item);
      window.open(url, "_blank", "noopener,noreferrer");
    }

    setAddedItems((prev) => {
      if (prev.includes(item)) return prev;
      return [...prev, item];
    });
  };

  const openAllItems = (provider, items) => {
    const list = Array.isArray(items) ? items.filter(Boolean) : [];
    if (list.length === 0) return;

    list.forEach((item, index) => {
      setTimeout(() => {
        handleOpenItem(provider, item);
      }, index * 700);
    });
  };

  const openOneItem = (provider, items) => {
    const list = Array.isArray(items) ? items.filter(Boolean) : [];
    if (list.length === 0) return;

    const item = list[0];
    handleOpenItem(provider, item);
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

  const finalizeShoppingSession = async () => {
    const user = auth.currentUser;
    if (!user) return;

    try {
      const data = await postJSON("http://127.0.0.1:8000/nutrition/shopping/adjust-plan", {
        user_id: user.uid,
      });
      setOutcomeMessage(String(data?.message || "").trim());
    } catch (e) {
      setOutcomeMessage(e?.message || "Could not adjust plan right now.");
    }
  };

  return (
    <div className="min-h-screen bg-black text-white">
      <TopNav />

      <div className="max-w-6xl mx-auto p-4 space-y-4">

      <h1 className="text-2xl font-bold">Your Nutrition Plan 🥗</h1>

      {proactiveSuggestion?.items?.length > 0 && (
        <div className="mb-4 rounded-lg border border-amber-400/40 bg-amber-900/20 px-4 py-3 text-sm text-amber-100">
          <div className="font-semibold">You're missing ingredients for next meals</div>
          <div className="mt-1">{proactiveSuggestion?.message}</div>
          <div className="mt-2 text-xs text-amber-200">Missing: {proactiveSuggestion.items.join(", ")}</div>
        </div>
      )}

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

      <div className="rounded-xl border border-zinc-700 bg-zinc-900/60 p-4 space-y-4">
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
            <textarea
              value={unavailableInput}
              onChange={(e) => setUnavailableInput(e.target.value)}
              placeholder="curd, paneer, roasted chana"
              className="mt-1 w-full min-h-[88px] rounded-lg bg-zinc-900 border border-zinc-700 p-2 text-sm"
            />
          </div>
        </div>

        <div className="grid md:grid-cols-2 gap-3">
          <div>
            <label className="text-xs text-gray-300">City</label>
            <input
              value={cityInput}
              onChange={(e) => setCityInput(e.target.value)}
              placeholder="hyderabad"
              className="mt-1 w-full rounded-lg bg-zinc-900 border border-zinc-700 p-2 text-sm"
            />
          </div>
          <div>
            <label className="text-xs text-gray-300">Budget note (optional)</label>
            <input
              value={budgetMessageInput}
              onChange={(e) => setBudgetMessageInput(e.target.value)}
              placeholder="Keep it under Rs 300"
              className="mt-1 w-full rounded-lg bg-zinc-900 border border-zinc-700 p-2 text-sm"
            />
          </div>
        </div>

        <div>
          <div className="text-xs text-gray-300 mb-2">Preferred providers</div>
          <div className="flex flex-wrap gap-3">
            {[
              ["blinkit", "Blinkit"],
              ["zepto", "Zepto"],              ["amazon", "Amazon"],
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

        {(formattedEstimatedCost || shoppingCoverageDays !== null || shoppingSuggestions.length > 0) && (
          <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/20 p-3 space-y-3">
            <div className="text-sm font-semibold text-emerald-200">Cart Insights</div>

            <div className="grid md:grid-cols-2 gap-2 text-sm">
              <div className="rounded-md border border-zinc-700 bg-zinc-900/70 p-2">
                <div className="text-xs text-zinc-400">Estimated Cost</div>
                <div className="font-medium text-emerald-300">{formattedEstimatedCost || "Not available"}</div>
              </div>
              <div className="rounded-md border border-zinc-700 bg-zinc-900/70 p-2">
                <div className="text-xs text-zinc-400">Coverage</div>
                <div className="font-medium text-emerald-300">
                  {shoppingCoverageDays !== null ? `${shoppingCoverageDays} day(s)` : "Not available"}
                </div>
              </div>
            </div>

            {detectedBudget !== null && (
              <div className="rounded-md border border-zinc-700 bg-zinc-900/70 p-2 text-sm">
                <div className="text-xs text-zinc-400">Budget</div>
                <div className="font-medium text-emerald-300">Rs {detectedBudget.toLocaleString()}</div>
                <div className="mt-1 text-xs text-zinc-300">
                  Cost vs budget: Rs {Number(shoppingPlan?.estimated_cost || 0).toLocaleString()} / Rs {detectedBudget.toLocaleString()}
                </div>
                <div className={`mt-1 text-xs ${withinBudget ? "text-emerald-300" : "text-amber-300"}`}>
                  {withinBudget ? "Fits within your budget" : "Adjusted to fit your budget"}
                </div>
              </div>
            )}

            {missingIngredients.length > 0 && (
              <div>
                <div className="text-xs text-zinc-400 mb-1">Missing ingredients identified</div>
                <div className="flex flex-wrap gap-2">
                  {missingIngredients.map((item, idx) => (
                    <span
                      key={`${item}-${idx}`}
                      className="text-xs px-2 py-1 rounded-md border border-zinc-700 bg-zinc-900"
                    >
                      {item}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {shoppingSuggestions.length > 0 && (
              <div>
                <div className="text-xs text-zinc-400 mb-1">Suggestions</div>
                <ul className="list-disc ml-5 text-sm text-emerald-100/90 space-y-1">
                  {shoppingSuggestions.map((suggestion, idx) => (
                    <li key={`suggestion-${idx}`}>{suggestion}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
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

        {bestProvider && (
          <div className="space-y-3">
            <div className="text-sm font-semibold">Recommended provider</div>

            <div className="rounded-lg border border-emerald-400/40 bg-emerald-950/20 p-4 space-y-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-xs uppercase tracking-wide text-emerald-200/90">Best match</div>
                  <div className="text-lg font-semibold text-emerald-100">{bestProvider.name}</div>
                  {selectionReason && (
                    <div className="mt-1 text-xs text-emerald-200/90">
                      {selectionReason}
                    </div>
                  )}
                </div>
                <div className="text-right text-sm">
                  <div className="text-zinc-400">Cost</div>
                  <div className="font-semibold text-emerald-200">Rs {Number(bestProvider.cost || 0).toLocaleString()}</div>
                  <div className="mt-1 text-zinc-400">Delivery</div>
                  <div className="font-semibold text-emerald-200">{bestProvider.delivery_time || "-"} min</div>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                <a
                  href="#"
                  onClick={(e) => {
                    e.preventDefault();
                    openOneItem(
                      bestProvider.name,
                      (shoppingPlan?.cart_items || []).map((item) => item?.item)
                    );
                  }}
                  className="px-3 py-1.5 rounded-md bg-emerald-500 text-black text-sm font-semibold"
                >
                  Open in {bestProvider.name}
                </a>
                <button
                  onClick={() => setGuidedMode(true)}
                  className="px-3 py-1.5 rounded-md bg-zinc-800 border border-zinc-700 text-sm font-semibold"
                >
                  Add items (guided)
                </button>
                <button
                  onClick={() => confirmShoppingOrder(bestProvider.name)}
                  disabled={confirmLoading}
                  className="px-3 py-1.5 rounded-md bg-zinc-800 border border-zinc-700 text-sm font-semibold disabled:opacity-60"
                >
                  {confirmLoading ? "Confirming..." : `Confirm ${bestProvider.name}`}
                </button>
              </div>

              <div className="flex flex-wrap gap-2">
                {(bestProvider.item_links || []).slice(0, 6).map((itemLink, itemIdx) => (
                  <button
                    key={`${itemLink.item}-${itemIdx}`}
                    onClick={() => handleOpenItem(bestProvider.name, itemLink.item)}
                    className="text-xs px-2 py-1 rounded-md border border-zinc-700 bg-zinc-900 hover:bg-zinc-800"
                  >
                    {itemLink.item}
                  </button>
                ))}
              </div>

              {guidedMode && (
                <div className="rounded-md border border-emerald-400/30 bg-black/30 p-3 space-y-3">
                  <h3 className="text-sm font-semibold">Add items step-by-step</h3>

                  {nextItem && (
                    <p className="text-xs text-emerald-300">Next: {nextItem}</p>
                  )}

                  <div className="space-y-2">
                    {uniqueItems.map((item) => {
                      const done = addedItems.includes(item);
                      return (
                        <div key={item} className="flex justify-between items-center gap-3 my-2">
                          <span className="text-sm">
                            {done ? "✅" : "⬜"} {item}
                          </span>
                          <button
                            disabled={done}
                            onClick={() => handleOpenItem(bestProvider.name, item)}
                            className="text-xs px-2 py-1 rounded-md bg-zinc-800 border border-zinc-600 disabled:opacity-50"
                          >
                            {done ? "Added" : "Open"}
                          </button>
                        </div>
                      );
                    })}
                  </div>

                  <div className="space-y-1">
                    <p className="text-xs text-zinc-300">{addedItems.length} / {uniqueItems.length} items added</p>
                    <div className="w-full h-2 bg-zinc-800 rounded-full">
                      <div
                        className="h-full bg-emerald-400 rounded-full transition-all duration-300"
                        style={{ width: `${progress}%` }}
                      />
                    </div>
                  </div>

                  {uniqueItems.length > 0 && addedItems.length === uniqueItems.length && (
                    <div className="text-sm text-emerald-300 font-semibold">
                      🎉 All items added! Ready to checkout
                    </div>
                  )}

                  {followupMessage && addedItems.length < uniqueItems.length && (
                    <div className="text-xs text-amber-300">{followupMessage}</div>
                  )}

                  <div className="flex gap-2">
                    <button
                      onClick={() =>
                        openAllItems(
                          bestProvider.name,
                          uniqueItems.filter((item) => !addedItems.includes(item))
                        )
                      }
                      className="text-xs px-2 py-1 rounded-md bg-zinc-800 border border-zinc-600"
                    >
                      Continue adding
                    </button>
                    <button
                      onClick={finalizeShoppingSession}
                      className="text-xs px-2 py-1 rounded-md bg-zinc-800 border border-zinc-600"
                    >
                      End session and adjust plan
                    </button>
                  </div>
                </div>
              )}
            </div>

            {alternatives.length > 0 && (
              <details className="rounded-lg border border-zinc-700 bg-zinc-900/60 p-3">
                <summary className="cursor-pointer text-sm font-medium text-zinc-200">See alternatives</summary>
                <div className="mt-3 space-y-2">
                  {alternatives.map((provider, idx) => (
                    <div
                      key={`${provider.name}-${idx}`}
                      className="rounded-md border border-zinc-700 bg-zinc-900/70 p-2 text-sm"
                    >
                      <div className="flex items-center justify-between gap-3">
                        <div className="font-medium">{provider.name}</div>
                        <div className="text-xs text-zinc-300">
                          Rs {Number(provider.cost || 0).toLocaleString()} | {provider.delivery_time || "-"} min
                        </div>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <button
                          onClick={() =>
                            window.open(provider.link, "_blank", "noopener,noreferrer")
                          }
                          className="text-xs px-2 py-1 rounded-md bg-zinc-800 border border-zinc-600"
                        >
                          Open in {provider.name}
                        </button>
                        <button
                          onClick={() =>
                            openAllItems(
                              provider.name,
                              uniqueItems
                            )
                          }
                          className="text-xs px-2 py-1 rounded-md bg-zinc-800 border border-zinc-600"
                        >
                          Add items
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </details>
            )}

            {shoppingPlan?.confirmation?.status && (
              <div className="text-sm text-green-300">
                Order action recorded: {shoppingPlan.confirmation.status} ({shoppingPlan.confirmation.provider})
              </div>
            )}

            {outcomeMessage && (
              <div className="text-sm text-amber-200 rounded-md border border-amber-400/30 bg-amber-900/20 p-2">
                {outcomeMessage}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        {visiblePlanDays.length > 0 ? (
          visiblePlanDays.map((day, i) => (
            <div key={`${day?.day || "day"}-${i}`} className="border border-zinc-700 bg-zinc-900/60 rounded-xl p-5">
              <h2 className="font-bold">{day.day}</h2>

              <Section title="Breakfast" items={day.breakfast} />
              <Section title="Lunch" items={day.lunch} />
              <Section title="Snacks" items={day.snacks} />
              <Section title="Dinner" items={day.dinner} />

              <p className="text-green-300 text-sm mt-3">💡 {day.tip}</p>
            </div>
          ))
        ) : (
          <div className="md:col-span-2 border border-zinc-700 rounded-xl p-5 bg-zinc-900/60">
            <h2 className="font-bold">No pending nutrition days in this cycle</h2>
            <p className="text-sm text-gray-300 mt-2">
              Great consistency. Ask Coach to refresh your next cycle nutrition plan.
            </p>
          </div>
        )}
      </div>
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
