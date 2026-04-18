import { useEffect, useState } from "react";
import TopNav from "../components/TopNav";
import { auth, db } from "../services/firebase";
import { collection, limit, onSnapshot, orderBy, query } from "firebase/firestore";

export default function Workouts() {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  let risk = null;

  try {
    risk = JSON.parse(localStorage.getItem("riskResult") || "null");
  } catch {}

  useEffect(() => {
    const user = auth.currentUser;

    // Fallback for local cache.
    let cached = null;
    try {
      cached = JSON.parse(localStorage.getItem("workoutPlan") || "null");
    } catch {}

    if (!user) {
      setPlan(cached);
      setLoading(false);
      return;
    }

    const q = query(
      collection(db, "users", user.uid, "workoutPlans"),
      orderBy("createdAt", "desc"),
      limit(1)
    );

    const unsub = onSnapshot(
      q,
      (snap) => {
        if (!snap.empty) {
          const latest = snap.docs[0].data();
          const resolved = { plan: latest.plan || [] };
          setPlan(resolved);
          try {
            localStorage.setItem("workoutPlan", JSON.stringify(resolved));
          } catch {}
        } else {
          setPlan(cached);
        }
        setLoading(false);
      },
      (e) => {
        console.error(e);
        setPlan(cached);
        setLoading(false);
      }
    );

    return () => unsub();
  }, []);

  if (loading) {
    return (
      <div className="min-h-screen bg-[radial-gradient(circle_at_top,#0f172a,#020617)] text-white">
        <TopNav rightText="Dashboard" onRightClick={() => (window.location.href = "/")} />
        <div className="max-w-4xl mx-auto px-4 py-24 text-center">Loading...</div>
      </div>
    );
  }

  // 🔴 No workout generated yet
  if (!plan || !plan.plan) {
    return (
      <div className="min-h-screen bg-[radial-gradient(circle_at_top,#0f172a,#020617)] text-white">
        <TopNav
          rightText="Dashboard"
          onRightClick={() => (window.location.href = "/")}
        />

        <div className="max-w-4xl mx-auto px-4 py-24 text-center">
          <h1 className="text-2xl font-semibold">No Workout Plan Yet</h1>
          <p className="text-white/70 mt-3">
            Go back to the dashboard and generate your personalized workout plan.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top,#0f172a,#020617)] text-white">
      <TopNav
        rightText="Update Form"
        onRightClick={() => (window.location.href = "/form")}
      />

      <div className="max-w-6xl mx-auto px-4 py-8">
        <h1 className="text-2xl font-semibold">Your Workout Plan 💪</h1>

        <p className="text-white/70 mt-2">
          {risk
            ? `Personalized for your ${risk.risk_level} risk profile`
            : "AI-generated workout plan"}
        </p>

        {/* 🔥 AI Workout Plan */}
        <div className="mt-6 grid grid-cols-1 md:grid-cols-2 gap-6">
          {plan.plan.map((day, idx) => (
            <Card
              key={idx}
              title={day.day}
              subtitle="AI Personalized Workout"
            >
              {/* Warmup */}
              <Section title="Warm-up">
                {day.warmup.map((w, i) => (
                  <WorkoutItem
                    key={i}
                    name={w}
                    meta="Warm-up"
                    text=""
                  />
                ))}
              </Section>

              {/* Exercises */}
              <Section title="Exercises">
                {day.exercises.map((ex, i) => (
                  <WorkoutItem
                    key={i}
                    name={ex.name}
                    meta={`${ex.sets} sets • ${ex.reps} reps`}
                    text={`Rest: ${ex.rest}`}
                  />
                ))}
              </Section>

              {/* Cooldown */}
              <Section title="Cool-down">
                {day.cooldown.map((c, i) => (
                  <WorkoutItem
                    key={i}
                    name={c}
                    meta="Cool-down"
                    text=""
                  />
                ))}
              </Section>

              {/* Tip */}
              {day.tip && (
                <div className="mt-4 text-sm text-green-300">
                  💡 {day.tip}
                </div>
              )}
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ---------- UI Components ---------- */

function Card({ title, subtitle, children }) {
  return (
    <div className="rounded-2xl p-6 bg-white/5 border border-white/10">
      <div className="text-lg font-semibold">{title}</div>
      <div className="text-white/60 text-sm mt-1">{subtitle}</div>
      <div className="mt-5">{children}</div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="mb-5">
      <div className="text-sm font-semibold text-white/80 mb-2">
        {title}
      </div>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

function WorkoutItem({ name, meta, text }) {
  return (
    <div className="rounded-xl p-4 bg-white/5 border border-white/10">
      <div className="flex items-center justify-between">
        <div className="font-semibold">{name}</div>
        <div className="text-xs text-white/60">{meta}</div>
      </div>
      {text && (
        <div className="text-white/70 text-sm mt-2">{text}</div>
      )}
    </div>
  );
}
