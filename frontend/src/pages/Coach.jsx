import { useEffect, useState } from "react";
import { onAuthStateChanged } from "firebase/auth";
import { collection, getDocs, limit, onSnapshot, orderBy, query } from "firebase/firestore";
import { auth, db } from "../services/firebase";
import TopNav from "../components/TopNav";
import { postJSON } from "../services/api";

function normalizeAssistantText(value) {
  if (typeof value !== "string") return "";

  const raw = value.trim();
  if (!raw) return "";

  const cleaned = raw
    .replace(/^```json\s*/i, "")
    .replace(/^```\s*/i, "")
    .replace(/```$/i, "")
    .trim();

  try {
    const parsed = JSON.parse(cleaned);
    if (parsed && typeof parsed === "object") {
      if (typeof parsed.ai_reply === "string" && parsed.ai_reply.trim()) return parsed.ai_reply.trim();
      if (typeof parsed.message === "string" && parsed.message.trim()) return parsed.message.trim();
      if (typeof parsed.summary === "string" && parsed.summary.trim()) return parsed.summary.trim();
    }
  } catch {
    // Heuristic extraction for malformed JSON-like blobs from LLM output.
    const extractField = (source, key) => {
      const keyNeedle = `"${key}"`;
      const keyPos = source.indexOf(keyNeedle);
      if (keyPos === -1) return "";

      const colonPos = source.indexOf(":", keyPos + keyNeedle.length);
      if (colonPos === -1) return "";

      const firstQuote = source.indexOf('"', colonPos + 1);
      if (firstQuote === -1) return "";

      let end = firstQuote + 1;
      let escaped = false;
      while (end < source.length) {
        const ch = source[end];
        if (ch === '"' && !escaped) break;
        escaped = ch === "\\" && !escaped;
        if (ch !== "\\") escaped = false;
        end += 1;
      }

      if (end >= source.length) return "";

      const rawValue = source.slice(firstQuote + 1, end);
      return rawValue
        .replace(/\\n/g, "\n")
        .replace(/\\"/g, '"')
        .replace(/\\\\/g, "\\")
        .trim();
    };

    const aiReply = extractField(cleaned, "ai_reply");
    if (aiReply) return aiReply;

    const message = extractField(cleaned, "message");
    if (message) return message;

    const summary = extractField(cleaned, "summary");
    if (summary) return summary;
  }

  return cleaned;
}

function toTwoDayPreview(planUpdates = {}, currentPlans = {}) {
  const workoutSource = planUpdates?.workout?.plan || currentPlans?.workout?.plan || [];
  const nutritionSource = planUpdates?.nutrition?.plan || currentPlans?.nutrition?.plan || [];

  return {
    workout: workoutSource.slice(0, 2),
    nutrition: nutritionSource.slice(0, 2),
  };
}

export default function Coach() {
  const [messages, setMessages] = useState([]);
  const [conversations, setConversations] = useState([]);
  const [activeConversationId, setActiveConversationId] = useState(null);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState("auto");
  const [autonomous, setAutonomous] = useState(false);
  const [showTrace, setShowTrace] = useState(false);

  useEffect(() => {
    const unsub = onAuthStateChanged(auth, async (user) => {
      if (!user) {
        setMessages([]);
        setConversations([]);
        setActiveConversationId(null);
        return;
      }

      try {
        const convQ = query(
          collection(db, "users", user.uid, "conversations"),
          orderBy("updatedAt", "desc"),
          limit(50)
        );

        const convSnap = await getDocs(convQ);
        const convs = convSnap.docs.map((d) => ({ id: d.id, ...d.data() }));
        setConversations(convs);

        if (convs.length > 0) {
          setActiveConversationId((prev) => prev || convs[0].id);
        }
      } catch (e) {
        console.error("Failed to load coach history", e);
      }
    });

    return () => unsub();
  }, []);

  useEffect(() => {
    const user = auth.currentUser;
    if (!user || !activeConversationId) {
      if (!activeConversationId) setMessages([]);
      return;
    }

    const q = query(
      collection(db, "users", user.uid, "conversations", activeConversationId, "messages"),
      orderBy("createdAt", "asc"),
      limit(300)
    );

    const unsub = onSnapshot(
      q,
      (snap) => {
        const items = snap.docs.map((d) => d.data());
        const mapped = items.map((msg) => {
          const payload = msg.payload || {};
          const role = msg.role === "assistant" ? "ai" : "user";
          if (role === "user") {
            return { role: "user", text: msg.text || "" };
          }

          const resolvedText =
            normalizeAssistantText(msg.text || "") ||
            normalizeAssistantText(payload.ai_reply || "") ||
            normalizeAssistantText(payload.summary || "") ||
            "I processed your update.";

          return {
            role: "ai",
            text: resolvedText,
            rawSummary: normalizeAssistantText(payload.summary || "") || "",
            agent: {
              actions: payload.actions || [],
              nudges: payload.nudges || [],
              decision: payload.decision || {},
              currentPlans: payload.current_plans || {},
              structuredLogs: payload.structured_logs || {},
              planUpdates: payload.plan_updates || {},
              weeklyReflection: payload.weekly_reflection || {},
              trace: payload.trace || [],
            },
          };
        });
        setMessages(mapped);
      },
      (e) => {
        console.error("Failed to load conversation messages", e);
      }
    );

    return () => unsub();
  }, [activeConversationId]);

  const callAgent = async (messageText, selectedMode = mode) => {
    if (!messageText.trim() && selectedMode === "auto") return;

    const user = auth.currentUser;
    if (!user) return alert("Not logged in");

    if (messageText.trim()) {
      const userMsg = { role: "user", text: messageText };
      setMessages((m) => [...m, userMsg]);
    }

    setLoading(true);
    try {
      const data = await postJSON("http://127.0.0.1:8000/agent/run", {
        user_id: user.uid,
        conversation_id: activeConversationId,
        message: messageText,
        mode: selectedMode,
        autonomous,
        context: {},
      });

      if (data?.conversation_id && !activeConversationId) {
        setActiveConversationId(data.conversation_id);
      }

      if (data?.conversation_id) {
        setConversations((prev) => {
          const exists = prev.some((c) => c.id === data.conversation_id);
          if (exists) return prev;
          return [{ id: data.conversation_id, title: (messageText || "New chat").slice(0, 60) }, ...prev];
        });
      }

      // Keep section pages in sync with the latest persisted plans.
      try {
        const workoutPlan = data?.current_plans?.workout?.plan;
        const nutritionPlan = data?.current_plans?.nutrition?.plan;

        if (Array.isArray(workoutPlan) && workoutPlan.length > 0) {
          localStorage.setItem("workoutPlan", JSON.stringify({ plan: workoutPlan }));
        }
        if (Array.isArray(nutritionPlan) && nutritionPlan.length > 0) {
          localStorage.setItem("nutritionPlan", JSON.stringify(nutritionPlan));
        }
      } catch {
        // Ignore local cache errors; Firestore remains source of truth.
      }

      const aiText =
        normalizeAssistantText(data.ai_reply || "") ||
        normalizeAssistantText(data.summary || "") ||
        "I processed your update.";
      void aiText;
    } catch (err) {
      alert(err?.message || "Agent request failed");
    } finally {
      setLoading(false);
    }
  };

  const sendMessage = async () => {
    const text = input;
    setInput("");
    await callAgent(text, mode);
  };

  const runModeWithoutMessage = async (selectedMode) => {
    await callAgent("", selectedMode);
  };

  return (
    <div className="min-h-screen bg-black text-white">
      <TopNav />

      <div className="max-w-6xl mx-auto p-4 grid md:grid-cols-[280px_1fr] gap-4">
        <div className="border border-zinc-800 rounded-xl bg-zinc-950 p-3 h-[85vh] overflow-auto">
          <div className="flex items-center justify-between mb-3">
            <div className="text-sm font-semibold text-zinc-300">Chats</div>
            <button
              onClick={() => {
                setActiveConversationId(null);
                setMessages([]);
              }}
              className="px-2 py-1 text-xs bg-zinc-800 border border-zinc-700 rounded-md hover:bg-zinc-700"
            >
              New Chat
            </button>
          </div>

          <div className="space-y-2">
            {conversations.map((c) => (
              <button
                key={c.id}
                onClick={() => setActiveConversationId(c.id)}
                className={`w-full text-left px-3 py-2 rounded-md border text-sm ${
                  activeConversationId === c.id
                    ? "bg-zinc-800 border-zinc-600"
                    : "bg-zinc-900 border-zinc-800 hover:bg-zinc-800"
                }`}
              >
                {(c.title || "New chat").toString()}
              </button>
            ))}
          </div>
        </div>

        <div>
        <h1 className="text-xl font-bold mb-4">AI Coach 🤖</h1>

        <div className="mb-4 p-3 rounded-xl border border-zinc-700 bg-zinc-900/70 space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-sm text-zinc-300">Mode</label>
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value)}
              className="bg-zinc-800 border border-zinc-700 rounded-md px-2 py-1 text-sm"
            >
              <option value="auto">auto</option>
              <option value="chat">chat</option>
              <option value="plan">plan</option>
              <option value="log">log</option>
              <option value="weekly_reflection">weekly_reflection</option>
            </select>

            <label className="flex items-center gap-2 text-sm text-zinc-300">
              <input
                type="checkbox"
                checked={autonomous}
                onChange={(e) => setAutonomous(e.target.checked)}
                className="accent-green-500"
              />
              autonomous
            </label>

            <button
              onClick={() => runModeWithoutMessage("weekly_reflection")}
              className="px-3 py-1 text-sm bg-zinc-800 border border-zinc-700 rounded-md hover:bg-zinc-700"
            >
              Run Weekly Reflection
            </button>

            <button
              onClick={() => runModeWithoutMessage("plan")}
              className="px-3 py-1 text-sm bg-zinc-800 border border-zinc-700 rounded-md hover:bg-zinc-700"
            >
              Refresh Plan
            </button>

            <button
              onClick={() => setShowTrace((v) => !v)}
              className="px-3 py-1 text-sm bg-zinc-800 border border-zinc-700 rounded-md hover:bg-zinc-700"
            >
              {showTrace ? "Hide Technical View" : "Show Technical View"}
            </button>
          </div>
        </div>

        <div className="space-y-3 mb-4">
          {messages.map((m, i) => (
  <div key={i} className={`w-full flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
    <div
      className={`max-w-[90%] p-4 rounded-2xl space-y-2 ${
        m.role === "user"
          ? "bg-green-500 text-black"
          : "bg-zinc-800 border border-zinc-700"
      }`}
    >
    {m.role === "ai" && <div className="text-xs text-zinc-400">Lifeline Coach</div>}
    <div className="leading-relaxed whitespace-pre-line">{m.text}</div>

    {/* Agent internals */}
    {showTrace && m.role === "ai" && m.agent && (
      <div className="space-y-3 border border-zinc-700 rounded-lg p-3 bg-zinc-900/60">
        {m.rawSummary && (
          <div className="text-xs text-zinc-400">
            Raw summary: {m.rawSummary}
          </div>
        )}

        <div className="flex flex-wrap gap-2 text-xs">
          {(m.agent.actions || []).map((action, idx) => (
            <span key={idx} className="px-2 py-1 rounded-md bg-zinc-700 text-zinc-100">
              {action}
            </span>
          ))}
        </div>

        {m.agent?.decision?.drift && (
          <div className="text-xs text-zinc-300">
            Drift: <span className="font-semibold">{m.agent.decision.drift.status || "unknown"}</span>
            {m.agent.decision.drift.reason ? ` - ${m.agent.decision.drift.reason}` : ""}
          </div>
        )}

        {m.agent?.decision?.recovery_mode?.enabled && (
          <div className="text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-md px-2 py-1">
            Recovery Mode: ON - {m.agent.decision.recovery_mode.reason}
          </div>
        )}

        {Object.keys(m.agent.structuredLogs || {}).length > 0 && (
          <div className="text-xs text-zinc-300">
            Parsed logs: {JSON.stringify(m.agent.structuredLogs)}
          </div>
        )}

        {Object.keys(m.agent.planUpdates || {}).length > 0 && (
          <div className="text-xs text-zinc-300">
            Plan updates: {Object.keys(m.agent.planUpdates).join(", ")}
          </div>
        )}

        {(() => {
          const preview = toTwoDayPreview(m.agent.planUpdates, m.agent.currentPlans);
          if (!preview.workout.length && !preview.nutrition.length) return null;

          return (
            <div className="grid md:grid-cols-2 gap-3 text-xs">
              {preview.workout.length > 0 && (
                <div className="rounded-md border border-zinc-700 p-2 bg-zinc-800/60">
                  <div className="font-semibold text-zinc-100 mb-1">
                    {m.agent?.decision?.recovery_mode?.enabled
                      ? "Recovery workout (next 1-2 days)"
                      : "Updated workout (next 1-2 days)"}
                  </div>
                  {preview.workout.map((d, idx) => (
                    <div key={idx} className="mb-1 text-zinc-300">
                      {d.day}: {Array.isArray(d.exercises) ? d.exercises.slice(0, 2).map((x) => x.name).join(", ") : "-"}
                    </div>
                  ))}
                </div>
              )}

              {preview.nutrition.length > 0 && (
                <div className="rounded-md border border-zinc-700 p-2 bg-zinc-800/60">
                  <div className="font-semibold text-zinc-100 mb-1">Updated nutrition (next 1-2 days)</div>
                  {preview.nutrition.map((d, idx) => (
                    <div key={idx} className="mb-1 text-zinc-300">
                      {d.day}: {Array.isArray(d.breakfast) ? d.breakfast[0] : "-"}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })()}

        {m.agent.weeklyReflection?.summary && (
          <div className="text-xs text-zinc-300 space-y-1">
            <div className="font-semibold text-zinc-100">Weekly reflection</div>
            <div>{m.agent.weeklyReflection.summary}</div>
            {(m.agent.weeklyReflection.problems || []).length > 0 && (
              <div>Problems: {m.agent.weeklyReflection.problems.join(", ")}</div>
            )}
            {m.agent.weeklyReflection.strategy_update && (
              <div>Strategy: {m.agent.weeklyReflection.strategy_update}</div>
            )}
          </div>
        )}

        {(m.agent.trace || []).length > 0 && (
          <div className="text-xs text-zinc-400 space-y-1">
            <div className="font-semibold text-zinc-200">Execution trace</div>
            {(m.agent.trace || []).map((step, idx) => (
              <div key={idx}>
                {step.name} [{step.status}] - {step.detail}
              </div>
            ))}
          </div>
        )}
      </div>
    )}

    </div>
  </div>
))}

          {loading && <div className="text-gray-400">Thinking…</div>}
        </div>

        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            className="flex-1 p-2 rounded-lg bg-zinc-900 border border-zinc-700"
            placeholder="Log your day, ask for plan changes, or request reflection..."
          />
          <button
            onClick={sendMessage}
            className="px-4 py-2 bg-green-500 text-black rounded-lg font-semibold"
          >
            Send
          </button>
        </div>
        </div>
      </div>
    </div>
  );
}
