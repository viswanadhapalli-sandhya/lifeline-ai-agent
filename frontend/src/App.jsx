import { BrowserRouter, Routes, Route } from "react-router-dom";

import Landing from "./components/Landing";
import HealthForm from "./pages/HealthForm";
import Dashboard from "./pages/Dashboard";

import Workouts from "./pages/Workouts";
import Nutrition from "./pages/Nutrition";
import AICoach from "./pages/Coach";
import Insights from "./pages/Insights";
import Suggestions from "./pages/Suggestions";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />

        {/* after login, you can route user to dashboard or form depending on your flow */}
        <Route path="/form" element={<HealthForm />} />
        <Route path="/dashboard" element={<Dashboard />} />

        {/* new pages opened from dashboard cards */}
        <Route path="/workouts" element={<Workouts />} />
        <Route path="/nutrition" element={<Nutrition />} />
        <Route path="/coach" element={<AICoach />} />
        <Route path="/suggestions" element={<Suggestions />} />
        <Route path="/insights" element={<Insights />} />
      </Routes>
    </BrowserRouter>
  );
}
