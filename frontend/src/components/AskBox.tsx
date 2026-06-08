import { useState } from "react";
import { askQuestion } from "../api";

const SUGGESTIONS = [
  "Summarize my Bedrock usage this week",
  "Which model cost the most this month?",
  "How many tokens did Claude use today?",
];

export function AskBox() {
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(question: string) {
    const text = question.trim();
    if (!text || loading) return;
    setLoading(true);
    setError(null);
    setAnswer(null);
    try {
      setAnswer(await askQuestion(text));
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
          placeholder="e.g. Why did Sonnet cost more than Haiku this week?"
          aria-label="Ask about your Bedrock usage"
        />
        <button className="btn btn--primary ask__btn" type="submit" disabled={loading}>
          {loading ? "Thinking…" : "Ask"}
        </button>
      </form>

      <div className="ask__suggest">
        {SUGGESTIONS.map((s) => (
          <button key={s} className="chip" onClick={() => { setQ(s); submit(s); }}>
            {s}
          </button>
        ))}
      </div>

      {error && <div className="banner banner--err">{error}</div>}
      {answer && <div className="ask__answer">{answer}</div>}
      <p className="ask__note">
        Answers come from a Bedrock agent grounded in your real usage data. A Bedrock
        Guardrail keeps it to usage &amp; cost topics. Each question incurs a small token cost.
      </p>
    </section>
  );
}
