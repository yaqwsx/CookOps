export type RuntimeAuthentication =
  | { provider: "dummy" }
  | { provider: "google"; googleClientId: string };

type RuntimeConfig = {
  authentication?: {
    provider?: unknown;
    googleClientId?: unknown;
  };
};

declare global {
  interface Window {
    COOKOPS_RUNTIME_CONFIG?: RuntimeConfig;
  }
}

function validGoogleClientId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 255 &&
    value === value.trim()
  );
}

export function runtimeAuthentication(): RuntimeAuthentication | null {
  const authentication = window.COOKOPS_RUNTIME_CONFIG?.authentication;
  if (authentication?.provider === "google") {
    return validGoogleClientId(authentication.googleClientId)
      ? { provider: "google", googleClientId: authentication.googleClientId }
      : null;
  }
  return authentication?.provider === "dummy" ? { provider: "dummy" } : null;
}
