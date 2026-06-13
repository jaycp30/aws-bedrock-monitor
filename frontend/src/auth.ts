// Cognito Hosted UI — Authorization Code flow with PKCE (public SPA client, no secret).
import { CONFIG } from "./config";

const TOKEN_KEY = "bm_id_token";
const ACCESS_TOKEN_KEY = "bm_access_token";
const EXP_KEY = "bm_id_exp";
const VERIFIER_KEY = "bm_pkce_verifier";
const STATE_KEY = "bm_oauth_state";

// aws.cognito.signin.user.admin lets the access token call Cognito self-service
// APIs (GetUser, ChangePassword) for the signed-in user only.
const SCOPES = "openid email profile aws.cognito.signin.user.admin";

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

function getStored(key: string): string | null {
  return sessionStorage.getItem(key) ?? localStorage.getItem(key);
}

function setStored(key: string, value: string) {
  sessionStorage.setItem(key, value);
  localStorage.removeItem(key);
}

function removeStored(key: string) {
  sessionStorage.removeItem(key);
  localStorage.removeItem(key);
}

export function getToken(): string | null {
  const token = getStored(TOKEN_KEY);
  const exp = Number(getStored(EXP_KEY) || 0);
  if (!token || Date.now() / 1000 > exp - 30) return null;
  return token;
}

export function getAccessToken(): string | null {
  const token = getStored(ACCESS_TOKEN_KEY);
  const exp = Number(getStored(EXP_KEY) || 0);
  if (!token || Date.now() / 1000 > exp - 30) return null;
  return token;
}

// Signed-in user's email, read from the ID token claims (no API call needed).
export function getEmail(): string | null {
  const token = getToken();
  if (!token) return null;
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(payload)).email ?? null;
  } catch {
    return null;
  }
}

export function logout() {
  removeStored(TOKEN_KEY);
  removeStored(ACCESS_TOKEN_KEY);
  removeStored(EXP_KEY);
  removeStored(VERIFIER_KEY);
  removeStored(STATE_KEY);
  const url =
    `${CONFIG.cognitoDomain}/logout?client_id=${CONFIG.clientId}` +
    `&logout_uri=${encodeURIComponent(CONFIG.redirectUri)}`;
  window.location.href = url;
}

export async function login() {
  const verifier = randomString();
  const state = randomString(32);
  setStored(VERIFIER_KEY, verifier);
  setStored(STATE_KEY, state);
  const challenge = base64url(await sha256(verifier));
  const url =
    `${CONFIG.cognitoDomain}/oauth2/authorize?response_type=code` +
    `&client_id=${CONFIG.clientId}` +
    `&redirect_uri=${encodeURIComponent(CONFIG.redirectUri)}` +
    `&scope=${encodeURIComponent(SCOPES)}` +
    `&state=${encodeURIComponent(state)}` +
    `&code_challenge_method=S256&code_challenge=${challenge}`;
  window.location.href = url;
}

// Exchange ?code= for tokens on the redirect landing. Returns true if signed in.
export async function handleRedirect(): Promise<boolean> {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  if (!code) return !!getToken();

  const returnedState = params.get("state") || "";
  const expectedState = getStored(STATE_KEY) || "";
  if (!returnedState || returnedState !== expectedState) {
    removeStored(VERIFIER_KEY);
    removeStored(STATE_KEY);
    window.history.replaceState({}, "", window.location.pathname);
    return false;
  }

  const verifier = getStored(VERIFIER_KEY) || "";
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
  setStored(TOKEN_KEY, data.id_token);
  setStored(ACCESS_TOKEN_KEY, data.access_token);
  setStored(EXP_KEY, String(Math.floor(Date.now() / 1000) + data.expires_in));
  removeStored(VERIFIER_KEY);
  removeStored(STATE_KEY);
  // Clean ?code= from the URL.
  window.history.replaceState({}, "", window.location.pathname);
  return true;
}
