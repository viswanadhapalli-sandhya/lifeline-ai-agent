import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import TopNav from "../components/TopNav";
import { auth } from "../services/firebase";

export default function Dashboard() {
  <h1>🔥 DASHBOARD LOADED 🔥</h1>

  const navigate = useNavigate();

  const risk = useMemo(() => {
    try {
      return JSON.parse(localStorage.getItem("riskResult") || "null");
    } catch {
      return null;
    }
  }, []);

async function handleWorkoutClick() {
  const user = auth.currentUser;
  if (!user) {
    alert("Please log in first.");
    return;
  }

  // 1️⃣ If workout already exists, just show it
  console.log("Workout card clicked"); // 👈 ADD THIS
  const existing = localStorage.getItem("workoutPlan");
  if (existing) {
    navigate("/workouts");
    return;
  }

  // 2️⃣ Get risk + user data (from localStorage / Firebase later)
  const risk = JSON.parse(localStorage.getItem("riskResult") || "{}");

  // Temporary fallback logic (you can refine later)
  const goal =
    risk?.risk_level === "High" ? "weight loss" : "general fitness";

  const fitness_level =
    risk?.risk_score > 60 ? "beginner" : "intermediate";

  const time_per_day =
    fitness_level === "beginner" ? 20 : 30;

  // 3️⃣ Call backend
  const res = await fetch("http://localhost:8000/workouts/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: user.uid,
      goal,
      location: "home",
      time_per_day,
      fitness_level,
      equipment: "none",
    }),
  });

  const plan = await res.json();

  // 4️⃣ Save + redirect
  localStorage.setItem("workoutPlan", JSON.stringify(plan));
  navigate("/workouts");
}
  const sparks = [
    "Walking 10 mins after meals improves blood sugar.",
    "Muscle burns calories even at rest.",
    "Hydration boosts focus by up to 20%.",
    "Consistency beats intensity—always.",
    "Sleep is the most underrated supplement.",
  ];

  const spark = useMemo(
    () => sparks[Math.floor(Math.random() * sparks.length)],
    []
  );

  // fallback values until we wire Firestore progress
  const workouts = 12;
  const completion = 85;
  const consistency = 9;

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top,#0f172a,#020617)] text-white">
      <TopNav rightText="New Update" onRightClick={() => navigate("/form")} />

      <div className="max-w-6xl mx-auto px-4 py-8 space-y-6">
        {/* Welcome */}
        <div className="rounded-2xl p-6 bg-white/5 border border-white/10 shadow-[0_0_35px_rgba(99,102,241,0.15)]">
          <h1 className="text-2xl font-semibold">Welcome back 👋</h1>
          <p className="text-white/70 mt-2">
            {risk
              ? `Latest risk level: ${risk.risk_level} • Risk score: ${risk.risk_score}/100`
              : "Fill your health form to generate personalized plans."}
          </p>
        </div>

        {/* Performance */}
        <div className="rounded-2xl p-6 bg-white/5 border border-white/10">
          <h2 className="text-lg font-semibold mb-4 text-center">
            Performance
          </h2>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <Metric title="Workouts" value={workouts} />
            <Metric
              title="Completion"
              value={`${completion}%`}
              highlight
            />
            <Metric title="Consistency" value={`${consistency}/10`} />
          </div>
        </div>

        {/* Actions */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
          <ActionCard
  icon="🏋️"
  title="Workout Plan"
  desc={risk ? "Start your 30-day routine + videos" : "Generate plan from your form"}
  cta="Start"
  onClick={() => {
    console.log("Workout card pressed");
    handleWorkoutClick();
  }}
/>


          <ActionCard
            icon="🥗"
            title="Nutrition Plan"
            desc={risk ? "Daily meals + recipe videos" : "Generate diet plan from your form"}
            cta="View"
            onClick={() => navigate("/nutrition")}
          />
          <ActionCard
            icon="🤖"
            title="AI Coach"
            desc="Ask anything about workouts & nutrition"
            cta="Open"
            onClick={() => navigate("/coach")}
          />
        </div>

        {/* Daily Spark */}
        <div className="rounded-2xl p-6 bg-gradient-to-br from-slate-900 to-black border border-white/10 text-center">
          <div className="text-sm text-indigo-200">⚡ Daily Spark</div>
          <p className="mt-2 text-white/80">{spark}</p>
        </div>

        {/* Risk details (optional) */}
        {risk && (
          <div className="rounded-2xl p-6 bg-white/5 border border-white/10">
            <h3 className="font-semibold mb-3">Contributing factors</h3>
            <ul className="list-disc pl-6 text-white/75 space-y-1">
              {(risk.contributing_factors || []).map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

function Metric({ title, value, highlight }) {
  return (
    <div
      className={[
        "rounded-2xl p-5 text-center border",
        highlight
          ? "bg-green-500/10 border-green-500/30 shadow-[0_0_22px_rgba(34,197,94,0.18)]"
          : "bg-white/5 border-white/10",
      ].join(" ")}
    >
      <div className="text-3xl font-bold">{value}</div>
      <div className="text-white/70 mt-1">{title}</div>
    </div>
  );
}

function ActionCard({ icon, title, desc, cta, onClick }) {
  return (
    <div className="rounded-2xl p-6 bg-white/5 border border-white/10 hover:translate-y-[-6px] hover:shadow-[0_20px_45px_rgba(34,197,94,0.18)] transition">
      <div className="text-4xl">{icon}</div>
      <h3 className="mt-3 text-lg font-semibold">{title}</h3>
      <p className="mt-2 text-white/70 text-sm">{desc}</p>

      {/* 🔴 SINGLE SOURCE OF TRUTH FOR CLICK */}
      <button
  type="button"
  onClick={() => alert("BUTTON CLICK WORKS")}
  className="mt-4 px-4 py-2 rounded-xl font-bold bg-gradient-to-r from-green-500 to-emerald-600 text-black"
>
  {cta}
</button>

    </div>
  );
}
