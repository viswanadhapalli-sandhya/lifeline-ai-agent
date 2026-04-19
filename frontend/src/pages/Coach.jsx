import { useEffect, useState } from "react";
import { onAuthStateChanged } from "firebase/auth";
import {
  collection,
  deleteDoc,
  doc,
  getDocs,
  limit,
  onSnapshot,
  orderBy,
  query,
  serverTimestamp,
  updateDoc,
  writeBatch,
} from "firebase/firestore";
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
  const [liveProgressSummary, setLiveProgressSummary] = useState(null);
  const [proactiveLoading, setProactiveLoading] = useState(false);
  const [proactiveData, setProactiveData] = useState(null);
  const [hasAutoProactiveRun, setHasAutoProactiveRun] = useState(false);
  const [menuConversationId, setMenuConversationId] = useState(null);
  const [renameConversationId, setRenameConversationId] = useState(null);
  const [renameInput, setRenameInput] = useState("");
  const [actionConversationId, setActionConversationId] = useState(null);

  const loadingMessage = proactiveLoading
    ? "Analyzing your habits..."
    : loading
      ? "Adjusting your plan..."
      : "";

  const conversationTitle = (conversation) => {
    const raw = String(conversation?.title || "").trim();
    return raw || "New chat";
  };

  const startRenameConversation = (conversation) => {
    setMenuConversationId(null);
    setRenameConversationId(conversation.id);
    setRenameInput(conversationTitle(conversation));
  };

  const cancelRenameConversation = () => {
    setRenameConversationId(null);
    setRenameInput("");
  };

  const submitRenameConversation = async (conversationId) => {
    const user = auth.currentUser;
    if (!user || !conversationId) return;

    const nextTitle = renameInput.trim().slice(0, 60);
    if (!nextTitle) {
      alert("Chat title cannot be empty");
      return;
    }

    setActionConversationId(conversationId);
    try {
      await updateDoc(doc(db, "users", user.uid, "conversations", conversationId), {
        title: nextTitle,
        updatedAt: serverTimestamp(),
      });
      cancelRenameConversation();
    } catch (e) {
      alert(e?.message || "Failed to rename chat");
    } finally {
      setActionConversationId(null);
    }
  };

  const deleteConversation = async (conversationId) => {
    const user = auth.currentUser;
    if (!user || !conversationId) return;

    const confirmed = window.confirm("Delete this chat and all its messages?");
    if (!confirmed) return;

    setMenuConversationId(null);
    setActionConversationId(conversationId);
    try {
      const messagesRef = collection(db, "users", user.uid, "conversations", conversationId, "messages");
      const messagesSnap = await getDocs(messagesRef);

      let batch = writeBatch(db);
      let ops = 0;
      for (const messageDoc of messagesSnap.docs) {
        batch.delete(messageDoc.ref);
        ops += 1;
        if (ops === 400) {
          await batch.commit();
          batch = writeBatch(db);
          ops = 0;
        }
      }
      if (ops > 0) {
        await batch.commit();
      }

      await deleteDoc(doc(db, "users", user.uid, "conversations", conversationId));

      if (activeConversationId === conversationId) {
        setActiveConversationId(null);
        setMessages([]);
      }
    } catch (e) {
      alert(e?.message || "Failed to delete chat");
    } finally {
      setActionConversationId(null);
    }
  };

  useEffect(() => {
    let unsubConversations = () => {};

    const unsub = onAuthStateChanged(auth, (user) => {
      unsubConversations();

      if (!user) {
        setMessages([]);
        setConversations([]);
        setActiveConversationId(null);
        return;
      }

      const convQ = query(
        collection(db, "users", user.uid, "conversations"),
        orderBy("updatedAt", "desc"),
        limit(50)
      );

      unsubConversations = onSnapshot(
        convQ,
        (convSnap) => {
          const convs = convSnap.docs.map((d) => ({ id: d.id, ...d.data() }));
          setConversations(convs);

          setActiveConversationId((prev) => {
            if (prev && convs.some((c) => c.id === prev)) return prev;
            return convs[0]?.id || null;
          });
        },
        (e) => {
          console.error("Failed to load coach history", e);
          setConversations([]);
        }
      );
    });

    return () => {
      unsubConversations();
      unsub();
    };
  }, []);

  useEffect(() => {
    if (!menuConversationId) return;

    const onDocumentClick = (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest("[data-conversation-actions='true']")) return;
      setMenuConversationId(null);
    };

    document.addEventListener("click", onDocumentClick);
    return () => document.removeEventListener("click", onDocumentClick);
  }, [menuConversationId]);

  useEffect(() => {
    let unsubProgress = () => {};

    const unsubAuth = onAuthStateChanged(auth, (user) => {
      unsubProgress();

      if (!user) {
        setLiveProgressSummary(null);
        return;
      }

      const summaryRef = doc(db, "users", user.uid, "progressStats", "summary");
      unsubProgress = onSnapshot(
        summaryRef,
        (snap) => {
          if (!snap.exists()) {
            setLiveProgressSummary(null);
            return;
          }
          setLiveProgressSummary(snap.data() || null);
        },
        () => {
          setLiveProgressSummary(null);
        }
      );
    });

    return () => {
      unsubProgress();
      unsubAuth();
    };
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
              data: payload.data || {},
              progressSummary: payload.progress_summary || {},
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

  const handleInputKeyDown = (e) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    e.preventDefault();
    if (!loading) {
      void sendMessage();
    }
  };

  const runModeWithoutMessage = async (selectedMode) => {
    await callAgent("", selectedMode);
  };

  const runProactiveCheck = async () => {
    const user = auth.currentUser;
    if (!user) return;

    setProactiveLoading(true);
    try {
      const data = await postJSON("http://127.0.0.1:8000/agent/proactive-check", {
        user_id: user.uid,
      });
      setProactiveData(data || null);
    } catch (e) {
      alert(e?.message || "Proactive check failed");
    } finally {
      setProactiveLoading(false);
    }
  };


  useEffect(() => {
    const user = auth.currentUser;
    if (!user || hasAutoProactiveRun) return;
    setHasAutoProactiveRun(true);
    void runProactiveCheck();
  }, [hasAutoProactiveRun]);

  const latestProgressSummary = (() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const msg = messages[i];
      const summary = msg?.agent?.progressSummary;
      if (summary && Object.keys(summary).length > 0) {
        return summary;
      }
    }
    return null;
  })();

  const resolvedProgressSummary =
    liveProgressSummary && Object.keys(liveProgressSummary).length > 0
      ? liveProgressSummary
      : latestProgressSummary;

  const progressWorkoutDays = Number(resolvedProgressSummary?.total_workout_days || 0);
  const currentCycleWeek = Math.floor(progressWorkoutDays / 7) + 1;
  const currentCycleDay = (progressWorkoutDays % 7) + 1;

  return (
    <div className="min-h-screen bg-black text-white">
      <TopNav />

      <div className="max-w-6xl mx-auto p-4 grid md:grid-cols-[280px_1fr] gap-4 items-start">
        <div className="sticky top-24 border border-zinc-800 rounded-xl bg-zinc-950 p-3 h-[calc(100vh-7rem)] flex flex-col self-start">
          <div className="flex items-center justify-between mb-3 shrink-0">
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

          <div className="space-y-2 overflow-y-auto pr-1">
            {conversations.map((c) => (
              <div
                key={c.id}
                className={`group relative rounded-md border ${
                  activeConversationId === c.id
                    ? "bg-zinc-800 border-zinc-600"
                    : "bg-zinc-900 border-zinc-800"
                }`}
              >
                {renameConversationId === c.id ? (
                  <div className="p-2 space-y-2" data-conversation-actions="true">
                    <input
                      autoFocus
                      value={renameInput}
                      onChange={(e) => setRenameInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          void submitRenameConversation(c.id);
                        }
                        if (e.key === "Escape") {
                          e.preventDefault();
                          cancelRenameConversation();
                        }
                      }}
                      className="w-full rounded-md bg-zinc-900 border border-zinc-700 px-2 py-1 text-sm"
                    />
                    <div className="flex gap-2">
                      <button
                        onClick={() => submitRenameConversation(c.id)}
                        disabled={actionConversationId === c.id}
                        className="px-2 py-1 text-xs rounded-md bg-zinc-700 hover:bg-zinc-600 disabled:opacity-60"
                      >
                        Save
                      </button>
                      <button
                        onClick={cancelRenameConversation}
                        disabled={actionConversationId === c.id}
                        className="px-2 py-1 text-xs rounded-md bg-zinc-800 border border-zinc-700 hover:bg-zinc-700 disabled:opacity-60"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setActiveConversationId(c.id)}
                      className={`flex-1 text-left px-3 py-2 text-sm truncate ${
                        activeConversationId === c.id ? "" : "hover:bg-zinc-800"
                      }`}
                    >
                      {conversationTitle(c)}
                    </button>

                    <div className="relative pr-1" data-conversation-actions="true">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setMenuConversationId((prev) => (prev === c.id ? null : c.id));
                        }}
                        className="px-2 py-1 rounded-md text-zinc-300 hover:bg-zinc-700 opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity"
                        aria-label="Chat actions"
                      >
                        ...
                      </button>

                      {menuConversationId === c.id && (
                        <div className="absolute right-1 top-9 z-10 w-32 rounded-md border border-zinc-700 bg-zinc-900 shadow-lg overflow-hidden">
                          <button
                            onClick={() => startRenameConversation(c)}
                            className="w-full px-3 py-2 text-left text-xs hover:bg-zinc-800"
                          >
                            Rename
                          </button>
                          <button
                            onClick={() => deleteConversation(c.id)}
                            disabled={actionConversationId === c.id}
                            className="w-full px-3 py-2 text-left text-xs text-rose-300 hover:bg-zinc-800 disabled:opacity-60"
                          >
                            Delete
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
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

            <button
              onClick={runProactiveCheck}
              disabled={proactiveLoading}
              className="px-3 py-1 text-sm bg-emerald-700/80 border border-emerald-500/40 rounded-md hover:bg-emerald-600/80 disabled:opacity-60"
            >
              {proactiveLoading ? "Running Proactive Check..." : "Run Proactive Check"}
            </button>
          </div>

          {loadingMessage && (
            <div className="text-xs text-cyan-200/90">{loadingMessage}</div>
          )}
        </div>

        {proactiveData && (
          <div className="mb-4 p-3 rounded-xl border border-emerald-700/40 bg-emerald-950/30 space-y-3">
            <div className="text-sm font-semibold text-emerald-200">Proactive Agent Recommendations</div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
              <div className="rounded-md border border-emerald-700/40 bg-emerald-900/20 p-2">
                <div className="text-emerald-300/80">Adherence (7d)</div>
                <div className="text-emerald-100 font-semibold">{Math.round((Number(proactiveData?.metrics?.adherence_rate_7d || 0) * 100))}%</div>
              </div>
              <div className="rounded-md border border-emerald-700/40 bg-emerald-900/20 p-2">
                <div className="text-emerald-300/80">Workout Days (7d)</div>
                <div className="text-emerald-100 font-semibold">{proactiveData?.metrics?.workout_days_7d || 0}</div>
              </div>
              <div className="rounded-md border border-emerald-700/40 bg-emerald-900/20 p-2">
                <div className="text-emerald-300/80">Meal Logs (7d)</div>
                <div className="text-emerald-100 font-semibold">{proactiveData?.metrics?.meal_log_days_7d || 0}</div>
              </div>
              <div className="rounded-md border border-emerald-700/40 bg-emerald-900/20 p-2">
                <div className="text-emerald-300/80">Active Streak</div>
                <div className="text-emerald-100 font-semibold">{proactiveData?.metrics?.active_streak_days || 0} day(s)</div>
              </div>
            </div>

            {Array.isArray(proactiveData?.recommendations) && proactiveData.recommendations.length > 0 ? (
              <div className="space-y-2">
                {proactiveData.recommendations.map((rec, idx) => (
                  <div key={`${rec.type || "rec"}-${idx}`} className="rounded-md border border-emerald-700/30 bg-black/20 p-2 text-xs space-y-1">
                    <div className="font-semibold text-emerald-100">{rec.title} ({rec.priority})</div>
                    <div className="text-emerald-200/80">Why this action: {rec.why_this_action || rec.reason}</div>
                    {typeof rec.confidence === "number" && rec.confidence > 0 && (
                      <div className="text-emerald-300/90">Confidence: {(rec.confidence * 100).toFixed(0)}%</div>
                    )}
                    <div className="text-emerald-300">Try: {rec.suggested_message}</div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-emerald-200/80">No proactive interventions needed right now.</div>
            )}

            {Array.isArray(proactiveData?.trends_7d) && proactiveData.trends_7d.length > 0 && (
              <div className="space-y-2">
                <div className="text-xs font-semibold text-emerald-200">7-day trend snapshot</div>
                <div className="grid grid-cols-7 gap-1">
                  {proactiveData.trends_7d.map((t) => (
                    <div key={t.date} className="rounded-md border border-emerald-700/30 bg-black/20 p-1.5 text-[10px]">
                      <div className="text-emerald-200/80 mb-1">{String(t.date).slice(5)}</div>
                      <div className={`h-2 rounded ${t.workout_completed ? "bg-emerald-400" : "bg-zinc-700"}`} title="Workout completed" />
                      <div className={`h-2 rounded mt-1 ${t.meal_logged ? "bg-teal-300" : "bg-zinc-700"}`} title="Meal logged" />
                      <div className="text-emerald-200/70 mt-1">{t.workout_minutes || 0}m</div>
                    </div>
                  ))}
                </div>
                <div className="text-[10px] text-emerald-200/70">Top bar = workout, middle bar = meal log.</div>
              </div>
            )}
          </div>
        )}

        {resolvedProgressSummary && (
          <div className="mb-4 p-3 rounded-xl border border-zinc-700 bg-zinc-900/70 space-y-2">
            <div className="text-sm font-semibold text-zinc-200">Progress Summary</div>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-xs">
              <div className="rounded-md border border-zinc-700 bg-zinc-800/60 p-2">
                <div className="text-zinc-400">Current Target</div>
                <div className="text-zinc-100 font-semibold">Week {currentCycleWeek} Day {currentCycleDay}</div>
              </div>
              <div className="rounded-md border border-zinc-700 bg-zinc-800/60 p-2">
                <div className="text-zinc-400">Workout Days</div>
                <div className="text-zinc-100 font-semibold">{resolvedProgressSummary.total_workout_days || 0}</div>
              </div>
              <div className="rounded-md border border-zinc-700 bg-zinc-800/60 p-2">
                <div className="text-zinc-400">Meal-log Days</div>
                <div className="text-zinc-100 font-semibold">{resolvedProgressSummary.total_meal_log_days || 0}</div>
              </div>
              <div className="rounded-md border border-zinc-700 bg-zinc-800/60 p-2">
                <div className="text-zinc-400">Total Logs</div>
                <div className="text-zinc-100 font-semibold">{resolvedProgressSummary.total_daily_logs || 0}</div>
              </div>
              <div className="rounded-md border border-zinc-700 bg-zinc-800/60 p-2">
                <div className="text-zinc-400">Workout Minutes</div>
                <div className="text-zinc-100 font-semibold">{resolvedProgressSummary.total_workout_minutes || 0}</div>
              </div>
            </div>

            {Array.isArray(resolvedProgressSummary.recent_workout_history) &&
              resolvedProgressSummary.recent_workout_history.length > 0 && (
                <div className="text-xs text-zinc-300 rounded-md border border-zinc-700 bg-zinc-800/40 p-2">
                  <div className="font-semibold text-zinc-200 mb-1">Recent Workout History</div>
                  {resolvedProgressSummary.recent_workout_history.slice(0, 5).map((entry, idx) => (
                    <div key={`${entry.date || "unknown"}-${idx}`}>
                      {(entry.date || "unknown")}: {entry.workout_minutes || 0} min
                    </div>
                  ))}
                </div>
              )}
          </div>
        )}

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

    {m.role === "ai" && typeof m.agent?.decision?.why_this_action === "string" && m.agent.decision.why_this_action.trim() && (
      <div className="text-xs text-amber-200/90 rounded-md border border-amber-500/30 bg-amber-500/10 p-2">
        Why this action: {m.agent.decision.why_this_action}
      </div>
    )}

    {m.role === "ai" && m.agent?.data?.simulation && (() => {
      const sim = m.agent.data.simulation;
      const recoveryPlan = Array.isArray(sim?.recovery_plan) ? sim.recovery_plan : [];

      return (
        <div className="text-xs rounded-md border border-cyan-500/30 bg-cyan-500/10 p-3 space-y-2">
          <div className="text-cyan-100 font-semibold">What-if Simulation</div>
          <div className="text-cyan-200/90">Impact: {String(sim?.impact || "unknown")}</div>
          <div className="text-cyan-200/90">Streak loss risk: {sim?.streak_loss ? "Yes" : "No"}</div>
          {recoveryPlan.length > 0 && (
            <div>
              <div className="text-cyan-100 mb-1">Recovery plan</div>
              <ul className="list-disc list-inside text-cyan-200/90 space-y-1">
                {recoveryPlan.map((step, idx) => (
                  <li key={`${step}-${idx}`}>{String(step)}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      );
    })()}

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
            onKeyDown={handleInputKeyDown}
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
