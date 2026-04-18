import { useEffect, useState } from "react";
import { auth, db } from "../services/firebase";
import { collection, doc, orderBy, query, limit, onSnapshot } from "firebase/firestore";
import TopNav from "../components/TopNav";

export default function Nutrition() {
  const [plan, setPlan] = useState(null);
  const [planMeta, setPlanMeta] = useState(null);
  const [progressSummary, setProgressSummary] = useState(null);
  const [loading, setLoading] = useState(true);

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

  const totalWorkoutDays = Number(progressSummary?.total_workout_days || 0);
  const completedDaysInCurrentCycle = totalWorkoutDays % 7;
  const visiblePlanDays = Array.isArray(plan) ? plan.slice(completedDaysInCurrentCycle) : [];

  if (loading) return <div className="text-white p-8">Loading...</div>;
  if (!plan) return <div className="text-white p-8">No nutrition plan yet</div>;

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

      {totalWorkoutDays > 0 && (
        <div className="mb-4 text-xs text-green-300/90">
          Completed in current cycle: {completedDaysInCurrentCycle} day(s)
        </div>
      )}

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
