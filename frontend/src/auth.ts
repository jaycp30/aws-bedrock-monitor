// Cognito Hosted UI — Authorization Code flow with PKCE (public SPA client, no secret).
import { CONFIG } from "./config";

const TOKEN_KEY = "bm_id_token";
const EXP_KEY = "bm_id_exp";
const VERIFIER_KEY = "bm_pkce_verifier";

function base64url(bytes: Uint8Array): string {
  let str = "";
  bytes.forEach((b) => (str += String.fromCharCode(b)));
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sha256(input: string): Promise<Uint8Array> {
  const data = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return new Uint8Array(digest);
}

function randomString(len = 64): string {
  const arr = new Uint8Array(len);
  crypto.getRandomValues(arr);
  return base64url(arr);
}

export function getToken(): string | null {
  const token = localStorage.getItem(TOKEN_KEY);
  const exp = Number(localStorage.getItem(EXP_KEY) || 0);
  if (!token || Date.now() / 1000 > exp - 30) return null;
  return token;
}

export function logout() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(EXP_KEY);
  const url =
    `${CONFIG.cognitoDomain}/logout?client_id=${CONFIG.clientId}` +
    `&logout_uri=${encodeURIComponent(CONFIG.redirectUri)}`;
  window.location.href = url;
}

export async function login() {
  const verifier = randomString();
  localStorage.setItem(VERIFIER_KEY, verifier);
  const challenge = base64url(await sha256(verifier));
  const url =
    `${CONFIG.cognitoDomain}/oauth2/authorize?response_type=code` +
    `&client_id=${CONFIG.clientId}` +
    `&redirect_uri=${encodeURIComponent(CONFIG.redirectUri)}` +
    `&scope=${encodeURIComponent("openid email profile")}` +
    `&code_challenge_method=S256&code_challenge=${challenge}`;
  window.location.href = url;
}

// Exchange ?code= for tokens on the redirect landing. Returns true if signed in.
export async function handleRedirect(): Promise<boolean> {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  if (!code) return !!getToken();

  const verifier = localStorage.getItem(VERIFIER_KEY) || "";
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: CONFIG.clientId,
    code,
    redirect_uri: CONFIG.redirectUri,
    code_verifier: verifier,
  });
  const resp = await fetch(`${CONFIG.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!resp.ok) return false;
  const data = await resp.json();
  localStorage.setItem(TOKEN_KEY, data.id_token);
  localStorage.setItem(EXP_KEY, String(Math.floor(Date.now() / 1000) + data.expires_in));
  localStorage.removeItem(VERIFIER_KEY);
  // Clean ?code= from the URL.
  window.history.replaceState({}, "", window.location.pathname);
  return true;
}
