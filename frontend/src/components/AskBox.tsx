import { useEffect, useRef, useState } from "react";
import { askChat, type ChatMessage } from "../api";

const SUGGESTIONS = [
  "Summarize my Bedrock usage this week",
  "Which model cost the most this month?",
  "How many tokens did Claude use today?",
];

// How many recent turns travel with each request. The backend enforces its
// own cap; this just keeps request size and token cost predictable.
const SENT_TURNS = 12;

interface AskBoxProps {
  range: string;
}

export function AskBox({ range }: AskBoxProps) {
  const [q, setQ] = useState("");
  const [thread, setThread] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [slowAsk, setSlowAsk] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);

  // Keep the newest message in view inside the thread's own scroll viewport
  // (the page itself never grows or jumps).
  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [thread, loading]);

  useEffect(() => {
    if (!loading) {
      setSlowAsk(false);
      return;
    }
    const timer = window.setTimeout(() => setSlowAsk(true), 4000);
    return () => window.clearTimeout(timer);
  }, [loading]);

  async function submit(question: string) {
    const text = question.trim();
    if (!text || loading) return;
    const nextThread: ChatMessage[] = [...thread, { role: "user", text }];
    setThread(nextThread);
    setQ("");
    setLoading(true);
    setError(null);
    try {
      const answer = await askChat(nextThread.slice(-SENT_TURNS), range);
      setThread([...nextThread, { role: "assistant", text: answer }]);
    } catch (e) {
      setThread(thread);
      setQ(text);
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card ask">
      <h2>Ask about your usage</h2>

      {thread.length > 0 && (
        <div className="ask__thread" ref={threadRef} aria-live="polite">
          {thread.map((m, i) => (
            <div key={i} className={`ask__msg ask__msg--${m.role}`}>
              {m.text}
            </div>
          ))}
          {loading && (
            <div className="ask__thinking" role="status" aria-label="Working on your question">
              <ThinkingMarks />
              <span>{slowAsk ? "Checking usage with Bedrock…" : "Thinking…"}</span>
            </div>
          )}
        </div>
      )}
      {thread.length === 0 && loading && (
        <div className="ask__thinking" role="status" aria-label="Working on your question">
          <ThinkingMarks />
          <span>{slowAsk ? "Checking usage with Bedrock…" : "Thinking…"}</span>
        </div>
      )}
      {error && <div className="banner banner--err">{error}</div>}

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

function ThinkingMarks() {
  return (
    <span className="ask__thinking-marks" aria-hidden="true">
      <span />
      <span />
      <span />
    </span>
  );
}
