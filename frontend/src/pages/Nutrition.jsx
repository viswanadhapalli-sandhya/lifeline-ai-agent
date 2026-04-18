import { useEffect, useState } from "react";
import { auth, db } from "../services/firebase";
import { collection, orderBy, query, limit, onSnapshot } from "firebase/firestore";
import TopNav from "../components/TopNav";

export default function Nutrition() {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const user = auth.currentUser;

    let cached = null;
    try {
      cached = JSON.parse(localStorage.getItem("nutritionPlan") || "null");
    } catch {}

    if (!user) {
      setPlan(cached);
      setLoading(false);
      return;
    }

    const q = query(
      collection(db, "users", user.uid, "nutritionPlans"),
      orderBy("createdAt", "desc"),
      limit(1)
    );

    const unsub = onSnapshot(
      q,
      (snap) => {
        if (!snap.empty) {
          const latestPlan = snap.docs[0].data().plan || [];
          setPlan(latestPlan);
          try {
            localStorage.setItem("nutritionPlan", JSON.stringify(latestPlan));
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

  if (loading) return <div className="text-white p-8">Loading...</div>;
  if (!plan) return <div className="text-white p-8">No nutrition plan yet</div>;

  return (
    <div className="min-h-screen bg-black text-white p-6">
      <TopNav />

      <h1 className="text-2xl font-bold mb-6">Your Nutrition Plan 🥗</h1>

      <div className="grid md:grid-cols-2 gap-6">
        {plan.map((day, i) => (
          <div key={i} className="border border-white/10 rounded-xl p-5">
            <h2 className="font-bold">{day.day}</h2>

            <Section title="Breakfast" items={day.breakfast} />
            <Section title="Lunch" items={day.lunch} />
            <Section title="Snacks" items={day.snacks} />
            <Section title="Dinner" items={day.dinner} />

            <p className="text-green-300 text-sm mt-3">💡 {day.tip}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function Section({ title, items }) {
  return (
    <div className="mt-3">
      <div className="text-sm font-semibold">{title}</div>
      <ul className="text-sm text-gray-300 list-disc ml-5">
        {items.map((i, idx) => (
          <li key={idx}>{i}</li>
        ))}
      </ul>
    </div>
  );
}
