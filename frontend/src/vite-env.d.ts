/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL: string;
  readonly VITE_COGNITO_DOMAIN: string;
  readonly VITE_CLIENT_ID: string;
  readonly VITE_REDIRECT_URI: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
