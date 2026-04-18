import { Link, useLocation } from "react-router-dom";
import { signOut } from "firebase/auth";
import { auth } from "../services/firebase";

export default function TopNav({ rightText = "Logout", onRightClick }) {
  const { pathname } = useLocation();

  const handleRightClick = async () => {
    if (typeof onRightClick === "function") {
      onRightClick();
      return;
    }

    try {
      await signOut(auth);
    } catch (e) {
      console.error("Logout failed", e);
    }

    window.location.href = "/";
  };

  const navItem = (to, label) => {
    const active = pathname === to;
    return (
      <Link
        to={to}
        className={[
          "px-3 py-2 rounded-xl text-sm font-medium transition",
          active
            ? "bg-white/10 text-white border border-white/10"
            : "text-white/70 hover:text-white hover:bg-white/5",
        ].join(" ")}
      >
        {label}
      </Link>
    );
  };

  return (
    <div className="w-full sticky top-0 z-20 backdrop-blur-xl bg-black/30 border-b border-white/10">
      <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
        <Link to="/dashboard" className="flex items-center gap-2">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-green-500 to-emerald-600 shadow-lg" />
          <div className="leading-tight">
            <div className="text-white font-semibold">Lifeline AI</div>
            <div className="text-white/60 text-xs">Wellness Dashboard</div>
          </div>
        </Link>

        <div className="flex items-center gap-2">
          {navItem("/dashboard", "Dashboard")}
          {navItem("/workouts", "Workouts")}
          {navItem("/nutrition", "Nutrition")}
          {navItem("/coach", "AI Coach")}

          <button
            onClick={handleRightClick}
            className="ml-2 px-3 py-2 rounded-xl text-sm font-semibold bg-white/10 hover:bg-white/15 border border-white/10 text-white"
          >
            {rightText}
          </button>
        </div>
      </div>
    </div>
  );
}
