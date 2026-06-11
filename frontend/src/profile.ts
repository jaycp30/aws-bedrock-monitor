// Cognito self-service profile actions, called directly from the browser with
// the user's access token (requires the aws.cognito.signin.user.admin scope).
// No backend involved — this talks to the regional Cognito IdP endpoint.
import { CONFIG } from "./config";
import { getAccessToken } from "./auth";

export class PasswordChangeError extends Error {}

// The hosted-UI domain embeds the region: <prefix>.auth.<region>.amazoncognito.com
function cognitoIdpEndpoint(): string {
  const match = CONFIG.cognitoDomain.match(/\.auth\.([a-z0-9-]+)\.amazoncognito\.com/);
  if (!match) throw new PasswordChangeError("Cognito domain is not configured.");
  return `https://cognito-idp.${match[1]}.amazonaws.com/`;
}

// Cognito error codes worth translating for the user; anything else falls back
// to a generic message (never surface raw exception types in the UI).
const FRIENDLY_ERRORS: Record<string, string> = {
  NotAuthorizedException: "Current password is incorrect.",
  InvalidPasswordException: "New password does not meet the password requirements.",
  LimitExceededException: "Too many attempts — wait a few minutes and try again.",
};

export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  const accessToken = getAccessToken();
  if (!accessToken) {
    throw new PasswordChangeError("Your session has expired. Sign out, sign back in, and retry.");
  }
  const resp = await fetch(cognitoIdpEndpoint(), {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": "AWSCognitoIdentityProviderService.ChangePassword",
    },
    body: JSON.stringify({
      PreviousPassword: currentPassword,
      ProposedPassword: newPassword,
      AccessToken: accessToken,
    }),
  });
  if (!resp.ok) {
    // Error shape: {"__type":"com.amazon...#NotAuthorizedException","message":"..."}
    let code = "";
    try {
      const body = await resp.json();
      code = String(body.__type ?? "").split("#").pop() ?? "";
    } catch {
      // non-JSON error body — fall through to the generic message
    }
    throw new PasswordChangeError(
      FRIENDLY_ERRORS[code] ?? "Password change failed. Try again or re-sign in.",
    );
  }
}
