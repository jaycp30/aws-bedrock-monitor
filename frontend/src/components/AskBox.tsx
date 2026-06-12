import { useState } from "react";
import Lottie from "lottie-react";
import { askChat, type ChatMessage } from "../api";
import thinkingAnimation from "../assets/ask-thinking.json";

const SUGGESTIONS = [
  "Summarize my Bedrock usage this week",
  "Which model cost the most this month?",
  "How many tokens did Claude use today?",
];

// How many recent turns travel with each request. The backend enforces its
// own cap; this just keeps request size and token cost predictable.
const SENT_TURNS = 12;

// Skip the animation for users who prefer reduced motion (they keep the text).
const REDUCED_MOTION =
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function AskBox() {
  const [q, setQ] = useState("");
  const [thread, setThread] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(question: string) {
    const text = question.trim();
    if (!text || loading) return;
    const nextThread: ChatMessage[] = [...thread, { role: "user", text }];
    setThread(nextThread);
    setQ("");
    setLoading(true);
    setError(null);
    try {
      const answer = await askChat(nextThread.slice(-SENT_TURNS));
      setThread([...nextThread, { role: "assistant", text: answer }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card ask">
      <h2>Ask about your usage</h2>
      <form
        className="ask__form"
        onSubmit={(e) => {
          e.preventDefault();
          submit(q);
        }}
      >
        <input
          className="ask__input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={
            thread.length === 0
              ? "e.g. Why did Sonnet cost more than Haiku this week?"
              : "Reply or ask something else…"
          }
          aria-label="Ask about your Bedrock usage"
        />
        <button className="btn btn--primary ask__btn" type="submit" disabled={loading}>
          Ask
        </button>
      </form>

      {thread.length === 0 && (
        <div className="ask__suggest">
          {SUGGESTIONS.map((s) => (
            <button key={s} className="chip" onClick={() => submit(s)}>
              {s}
            </button>
          ))}
        </div>
      )}

      {thread.length > 0 && (
        <div className="ask__thread" aria-live="polite">
          {thread.map((m, i) => (
            <div key={i} className={`ask__msg ask__msg--${m.role}`}>
              {m.text}
            </div>
          ))}
        </div>
      )}

      {loading && (
        <div className="ask__thinking" role="status" aria-label="Working on your question">
          {!REDUCED_MOTION && (
            <Lottie animationData={thinkingAnimation} loop className="ask__thinking-anim" />
          )}
          <span>Thinking…</span>
        </div>
      )}
      {error && <div className="banner banner--err">{error}</div>}

      {thread.length > 0 && !loading && (
        <button className="ask__clear" onClick={() => { setThread([]); setError(null); }}>
          Clear conversation
        </button>
      )}

      <p className="ask__note">
        Answers come from a Bedrock agent grounded in your real usage data. A Bedrock
        Guardrail keeps it to usage &amp; cost topics. Each question incurs a small token cost.
      </p>
    </section>
  );
}
