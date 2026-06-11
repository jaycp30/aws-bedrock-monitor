import { useState, type FormEvent } from "react";
import { getEmail } from "../auth";
import { changePassword, PasswordChangeError } from "../profile";

// Mirrors the Cognito user-pool policy (template.yaml): 12+ chars with
// lowercase, uppercase, number, and symbol. Checked client-side for fast
// feedback; Cognito re-validates server-side.
const PASSWORD_POLICY = /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{12,}$/;
const POLICY_HINT = "At least 12 characters with upper case, lower case, a number, and a symbol.";

interface FormMessage {
  kind: "ok" | "err";
  text: string;
}

export function ProfileMenu() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<FormMessage | null>(null);
  const email = getEmail();

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next !== confirm) {
      setMessage({ kind: "err", text: "New passwords do not match." });
      return;
    }
    if (!PASSWORD_POLICY.test(next)) {
      setMessage({ kind: "err", text: POLICY_HINT });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      await changePassword(current, next);
      setMessage({ kind: "ok", text: "Password updated." });
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      setMessage({
        kind: "err",
        text: err instanceof PasswordChangeError ? err.message : "Something went wrong.",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="profile">
      <button
        className="btn btn--ghost"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen(!open)}
      >
        Profile
      </button>
      {open && (
        <div className="profile__panel" role="dialog" aria-label="Your profile">
          <div className="profile__row">
            <span className="profile__label">Signed in as</span>
            <span className="profile__email">{email ?? "unknown"}</span>
          </div>
          <form className="profile__form" onSubmit={submit}>
            <label className="field">
              <span className="field__label">Current password</span>
              <input
                type="password"
                className="field__input"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
            <label className="field">
              <span className="field__label">New password</span>
              <input
                type="password"
                className="field__input"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                autoComplete="new-password"
                required
              />
            </label>
            <label className="field">
              <span className="field__label">Confirm new password</span>
              <input
                type="password"
                className="field__input"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
                required
              />
            </label>
            <p className="profile__hint">{POLICY_HINT}</p>
            {message && (
              <p className={`profile__msg profile__msg--${message.kind}`} role="status">
                {message.text}
              </p>
            )}
            <button className="btn btn--primary" type="submit" disabled={busy}>
              {busy ? "Updating…" : "Update password"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
