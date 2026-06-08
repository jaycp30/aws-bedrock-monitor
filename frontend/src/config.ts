// Build-time config, injected by Vite from .env.production (written by deploy.sh
// from the SAM stack outputs). For `npm run dev` against a deployed backend,
// create frontend/.env.local with the same VITE_* keys.
export const CONFIG = {
  apiUrl: import.meta.env.VITE_API_URL ?? "",
  cognitoDomain: import.meta.env.VITE_COGNITO_DOMAIN ?? "",
  clientId: import.meta.env.VITE_CLIENT_ID ?? "",
  redirectUri: import.meta.env.VITE_REDIRECT_URI ?? window.location.origin + "/",
};
