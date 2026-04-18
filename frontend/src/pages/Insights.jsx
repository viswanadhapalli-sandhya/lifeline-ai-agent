import { useEffect, useState } from "react";
import { onAuthStateChanged } from "firebase/auth";

import TopNav from "../components/TopNav";
import { auth } from "../services/firebase";
import { postJSON } from "../services/api";

export default function Insights() {
  const [loading, setLoading] = useState(true);
  const [metricsData, setMetricsData] = useState(null);
  const [proactiveData, setProactiveData] = useState(null);

  const loadInsights = async (uid) => {
    setLoading(true);
    try {
      const [metrics, proactive] = await Promise.all([
        postJSON("http://127.0.0.1:8000/agent/metrics", { user_id: uid }),
        postJSON("http://127.0.0.1:8000/agent/proactive-check", { user_id: uid }),
      ]);
      setMetricsData(metrics || null);
      setProactiveData(proactive || null);
    } catch (e) {
      alert(e?.message || "Failed to load insights");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const unsub = onAuthStateChanged(auth, (user) => {
      if (!user) {
        setMetricsData(null);
        setProactiveData(null);
        setLoading(false);
        return;
      }
      void loadInsights(user.uid);
    });

    return () => unsub();
  }, []);

  if (loading) {
    return (
      <div className="min-h-screen bg-black text-white">
        <TopNav />
        <div className="max-w-6xl mx-auto p-4">Loading insights...</div>
      </div>
    );
  }

  const m = metricsData?.metrics || {};
  const trends = metricsData?.trends || metricsData?.trends_7d || proactiveData?.trends_7d || [];
  const narratives = Array.isArray(metricsData?.narratives) ? metricsData.narratives : [];
  const trendComparison = metricsData?.trend_comparison_7d || {};

  return (
    <div className="min-h-screen bg-black text-white">
      <TopNav />

      <div className="max-w-6xl mx-auto p-4 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-2xl font-bold">Agent Insights</h1>
          <button
            onClick={() => {
              const user = auth.currentUser;
              if (user) void loadInsights(user.uid);
            }}
            className="px-3 py-2 rounded-lg bg-emerald-600 text-black font-semibold"
          >
            Refresh Insights
          </button>
        </div>

        <div className="grid md:grid-cols-4 gap-3 text-sm">
          <StatCard
            title="Adherence (7d)"
            value={`${Math.round(Number(m.adherence_rate_7d || 0) * 100)}%`}
            trend={trendComparison?.adherence_rate_7d}
          />
          <StatCard
            title="Active Streak"
            value={`${m.active_streak_days || 0} day(s)`}
            trend={trendComparison?.active_streak_days}
          />
          <StatCard
            title="Plan Refreshes (7d)"
            value={`${m.plan_refreshes_7d || 0}`}
            trend={trendComparison?.plan_refreshes_7d}
          />
          <StatCard
            title="Shopping Confirms (7d)"
            value={`${m.shopping_confirmations_7d || 0}`}
            trend={trendComparison?.shopping_confirmations_7d}
          />
        </div>

        <div className="rounded-xl border border-zinc-700 bg-zinc-900/60 p-4 space-y-3">
          <div className="text-lg font-semibold">Narrative Insights</div>
          {narratives.length > 0 ? (
            <div className="grid md:grid-cols-2 gap-3">
              {narratives.map((item, idx) => {
                const narrativeType = String(item?.type || "warning").toLowerCase();
                const style = narrativeStyleByType[narrativeType] || narrativeStyleByType.warning;
                return (
                  <div
                    key={`${narrativeType}-${idx}`}
                    className={`rounded-lg border p-3 text-sm ${style.border} ${style.bg}`}
                  >
                    <div className="flex items-center gap-2 font-semibold">
                      <span className={style.iconColor}>{style.icon}</span>
                      <span className={style.titleColor}>{style.label}</span>
                    </div>
                    <div className="mt-2 text-zinc-200">{item?.text || "No narrative available."}</div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-sm text-zinc-400">Narratives will appear once enough activity is available.</div>
          )}
        </div>

        <div className="rounded-xl border border-zinc-700 bg-zinc-900/60 p-4 space-y-3">
          <div className="text-lg font-semibold">7-day Trends</div>
          {Array.isArray(trends) && trends.length > 0 ? (
            <div className="grid grid-cols-7 gap-2">
              {trends.map((t) => (
                <div key={t.date} className="rounded-md border border-zinc-700 bg-zinc-950 p-2 text-xs">
                  <div className="text-zinc-400">{String(t.date).slice(5)}</div>
                  <div className={`h-2 rounded mt-1 ${t.workout_completed ? "bg-emerald-400" : "bg-zinc-700"}`} title="Workout completed" />
                  <div className={`h-2 rounded mt-1 ${t.meal_logged ? "bg-cyan-300" : "bg-zinc-700"}`} title="Meal logged" />
                  <div className="text-zinc-300 mt-1">{t.workout_minutes || 0} min</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-sm text-zinc-400">No trend data yet.</div>
          )}
          <div className="text-xs text-zinc-500">Bars: workout completed, meal logged.</div>
        </div>

        <div className="rounded-xl border border-zinc-700 bg-zinc-900/60 p-4 space-y-3">
          <div className="text-lg font-semibold">Proactive Recommendations</div>
          {Array.isArray(proactiveData?.recommendations) && proactiveData.recommendations.length > 0 ? (
            <div className="space-y-2">
              {proactiveData.recommendations.map((rec, idx) => (
                <div key={`${rec.type || "rec"}-${idx}`} className="rounded-md border border-zinc-700 bg-zinc-950 p-3 text-sm">
                  <div className="font-semibold text-emerald-200">{rec.title} ({rec.priority})</div>
                  <div className="text-zinc-300 mt-1">{rec.reason}</div>
                  <div className="text-emerald-300 mt-1">Try: {rec.suggested_message}</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-sm text-zinc-400">No interventions needed right now.</div>
          )}
        </div>
      </div>
    </div>
  );
}

const narrativeStyleByType = {
  positive: {
    icon: "●",
    label: "Positive",
    border: "border-emerald-500/60",
    bg: "bg-emerald-900/30",
    iconColor: "text-emerald-300",
    titleColor: "text-emerald-200",
  },
  warning: {
    icon: "●",
    label: "Warning",
    border: "border-yellow-500/60",
    bg: "bg-yellow-900/20",
    iconColor: "text-yellow-300",
    titleColor: "text-yellow-200",
  },
  critical: {
    icon: "●",
    label: "Critical",
    border: "border-red-500/60",
    bg: "bg-red-900/20",
    iconColor: "text-red-300",
    titleColor: "text-red-200",
  },
};

function StatCard({ title, value, trend }) {
  const direction = String(trend?.direction || "flat").toLowerCase();
  const trendDelta = Number(trend?.delta || 0);

  const arrow = direction === "up" ? "↑" : direction === "down" ? "↓" : "→";
  const arrowColor = direction === "up" ? "text-emerald-300" : direction === "down" ? "text-red-300" : "text-zinc-400";
  const deltaPrefix = trendDelta > 0 ? "+" : "";

  return (
    <div className="rounded-xl border border-zinc-700 bg-zinc-900/60 p-3">
      <div className="text-zinc-400 text-xs">{title}</div>
      <div className="text-zinc-100 text-lg font-semibold mt-1">{value}</div>
      <div className={`text-xs mt-1 ${arrowColor}`}>
        {arrow} {deltaPrefix}
        {Number.isFinite(trendDelta) ? trendDelta : 0} vs previous 7d
      </div>
    </div>
  );
}
