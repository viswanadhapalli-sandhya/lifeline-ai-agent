import { useEffect, useState } from "react";
import { onAuthStateChanged } from "firebase/auth";
import { collection, limit, onSnapshot, orderBy, query } from "firebase/firestore";

import TopNav from "../components/TopNav";
import { auth, db } from "../services/firebase";

function formatSuggestionTime(createdAt) {
  if (!createdAt) return "";
  try {
    if (typeof createdAt?.toDate === "function") {
      return createdAt.toDate().toLocaleString();
    }
    if (createdAt instanceof Date) {
      return createdAt.toLocaleString();
    }
  } catch {
    return "";
  }
  return "";
}

export default function Suggestions() {
  const [coachSuggestions, setCoachSuggestions] = useState([]);

  useEffect(() => {
    let unsubSuggestions = () => {};

    const unsubAuth = onAuthStateChanged(auth, (user) => {
      unsubSuggestions();

      if (!user) {
        setCoachSuggestions([]);
        return;
      }

      const proactiveEventsQ = query(
        collection(db, "users", user.uid, "agentEvents"),
        orderBy("createdAt", "desc"),
        limit(40)
      );

      unsubSuggestions = onSnapshot(
        proactiveEventsQ,
        (snap) => {
          const suggestions = snap.docs
            .map((d) => ({ id: d.id, ...(d.data() || {}) }))
            .filter((event) => {
              const eventType = String(event.type || "").toLowerCase();
              return eventType === "proactive" || eventType === "proactive_suggestion";
            })
            .map((event) => ({
              id: event.id,
              action: String(event.action || "general_coaching"),
              priority: String(event.priority || "medium").toLowerCase(),
              message: String(event.message || "").trim(),
              why: String(event.why_this_action || event.reason || "").trim(),
              confidence: Number(event.confidence || event?.decision?.confidence || 0),
              createdAt: event.createdAt || null,
            }));

          setCoachSuggestions(suggestions);
        },
        () => {
          setCoachSuggestions([]);
        }
      );
    });

    return () => {
      unsubSuggestions();
      unsubAuth();
    };
  }, []);

  return (
    <div className="min-h-screen bg-black text-white">
      <TopNav />

      <div className="max-w-6xl mx-auto p-4 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-2xl font-bold">Coach Suggestions</h1>
          <div className="text-xs text-zinc-400">Latest proactive interventions from your coach loop</div>
        </div>

        {coachSuggestions.length === 0 ? (
          <div className="rounded-xl border border-zinc-700 bg-zinc-900/60 p-4 text-sm text-zinc-300">
            No proactive suggestions yet. The autonomous coach loop will populate this page as interventions are generated.
          </div>
        ) : (
          <div className="space-y-3">
            {coachSuggestions.map((item) => {
              const isHigh = item.priority === "high";
              const toneClass = isHigh
                ? "border-rose-400/60 bg-rose-500/10"
                : "border-cyan-700/30 bg-zinc-900/60";

              return (
                <div key={item.id} className={`rounded-xl border p-3 text-sm space-y-2 ${toneClass}`}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="font-semibold text-cyan-100">{item.action.replaceAll("_", " ")}</div>
                    <div className={`uppercase tracking-wide text-xs ${isHigh ? "text-rose-200" : "text-cyan-200/90"}`}>
                      {item.priority}
                    </div>
                  </div>

                  {item.message && <div className="text-zinc-100">{item.message}</div>}
                  {item.why && <div className="text-zinc-300">Why this action: {item.why}</div>}
                  {item.confidence > 0 && (
                    <div className="text-cyan-300/90 text-xs">Confidence: {(item.confidence * 100).toFixed(0)}%</div>
                  )}

                  <div className="text-[11px] text-zinc-400">
                    {formatSuggestionTime(item.createdAt) || "Just now"}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
