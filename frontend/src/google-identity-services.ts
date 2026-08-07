export type GoogleCredentialResponse = {
  credential: string;
};

export type GoogleIdentityServices = {
  accounts: {
    id: {
      initialize: (configuration: {
        client_id: string;
        callback: (response: GoogleCredentialResponse) => void;
      }) => void;
      renderButton: (
        element: HTMLElement,
        options: {
          theme: "outline";
          size: "large";
          text: "signin_with";
          locale: string;
        },
      ) => void;
    };
  };
};

declare global {
  interface Window {
    google?: GoogleIdentityServices;
  }
}

const scriptId = "google-identity-services";
const scriptSource = "https://accounts.google.com/gsi/client";

function installedServices(): GoogleIdentityServices | null {
  return window.google?.accounts.id ? window.google : null;
}

export async function loadGoogleIdentityServices(): Promise<GoogleIdentityServices> {
  const installed = installedServices();
  if (installed) return installed;

  const existing = document.getElementById(
    scriptId,
  ) as HTMLScriptElement | null;
  const script = existing ?? document.createElement("script");
  if (!existing) {
    script.id = scriptId;
    script.src = scriptSource;
    script.async = true;
    document.head.append(script);
  }

  await new Promise<void>((resolve, reject) => {
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener(
      "error",
      () => {
        script.remove();
        reject(new Error("Google Identity Services failed to load."));
      },
      { once: true },
    );
  });
  const services = installedServices();
  if (!services) throw new Error("Google Identity Services is unavailable.");
  return services;
}
