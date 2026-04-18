import { useNavigate } from "react-router-dom";
import { useState } from "react";
import { loginWithGoogle } from "../services/authService";
import { db } from "../services/firebase";
import { collection, getDocs, limit, query } from "firebase/firestore";
import dashboardPreview from "../components/dashboard-preview.png";
import "./Landing.css";

const Landing = () => {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);

  const routeAfterLogin = async (uid) => {
    const q = query(collection(db, "users", uid, "healthRecords"), limit(1));
    const snap = await getDocs(q);

    if (snap.empty) {
      navigate("/form");
      return;
    }

    // Existing users go straight to agent (coach)
    navigate("/coach");
  };

  const handleGetStarted = async () => {
    if (loading) return;
    setLoading(true);

    try {
      const { idToken, user } = await loginWithGoogle();
      console.log("Firebase ID Token:", idToken);
      await routeAfterLogin(user.uid);
    } catch (err) {
      console.error("Google login failed:", err);
      alert(err?.message || "Google login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="landing">
      {/* HEADER */}
      <header className="header">
        <div className="container header-inner">
          <h1 className="logo">LifeLine AI</h1>

          <nav className="nav">
            <a href="#features">Features</a>
            <a href="#how">How it Works</a>

            <button onClick={handleGetStarted} disabled={loading}>
              {loading ? "Loading..." : "Login"}
            </button>

            <button
              className="btn-primary"
              onClick={handleGetStarted}
              disabled={loading}
            >
              {loading ? "Loading..." : "Get Started"}
            </button>
          </nav>
        </div>
      </header>

      {/* HERO */}
      <section className="hero">
        <div className="container hero-grid">
          <div>
            <span className="badge">AI-Powered Wellness Platform</span>

            <h2>
              Smarter Fitness.
              <br />
              Healthier Living.
            </h2>

            <p>
              Personalized workout and nutrition plans that adapt in real time
              with an intelligent AI health companion.
            </p>

            <div className="hero-actions">
              <button
                className="btn-primary"
                onClick={handleGetStarted}
                disabled={loading}
              >
                {loading ? "Loading..." : "Get Started Free"}
              </button>
            </div>
          </div>

          {/* ✅ IMAGE INSIDE APP PREVIEW BOX */}
          <div className="preview-card">
            <img
              src={dashboardPreview}
              alt="LifeLine AI Dashboard Preview"
              className="preview-image"
            />
          </div>
        </div>
      </section>

      {/* FEATURES */}
      <section id="features" className="features">
        <div className="container">
          <h3>Everything you need to stay healthy</h3>

          <div className="feature-grid">
            <Feature
              title="Personalized Workouts"
              text="AI-generated routines tailored to your goals, fitness level, and schedule."
            />
            <Feature
              title="Smart Nutrition"
              text="Meal plans adapted to calories, dietary preferences, and allergies."
            />
            <Feature
              title="AI Coach"
              text="Real-time guidance for travel, injuries, or busy days."
            />
            <Feature
              title="Progress Tracking"
              text="Visual insights to track consistency and long-term improvement."
            />
          </div>
        </div>
      </section>

      {/* HOW IT WORKS */}
      <section id="how" className="how">
        <div className="container">
          <h3>How It Works</h3>
          <ol>
            <li>Login</li>
            <li>Complete health assessment</li>
            <li>Get AI-generated plans</li>
            <li>Track and improve</li>
          </ol>
        </div>
      </section>

      {/* CTA */}
      <section className="cta">
        <h3>Start your wellness journey today</h3>
        <p>Simple. Personalized. Adaptive.</p>

        <button
          className="btn-primary"
          onClick={handleGetStarted}
          disabled={loading}
        >
          {loading ? "Loading..." : "Create Free Account"}
        </button>
      </section>

      {/* FOOTER */}
      <footer className="footer">
        <div className="container footer-inner">
          <p>© 2026 LifeLine AI</p>
          <div>
            <a>Privacy</a>
            <a>Terms</a>
            <a>Contact</a>
          </div>
        </div>
      </footer>
    </div>
  );
};

const Feature = ({ title, text }) => (
  <div className="feature-card">
    <div className="icon">●</div>
    <h4>{title}</h4>
    <p>{text}</p>
  </div>
);

export default Landing;
