import i18n from "i18next";
import { initReactI18next } from "react-i18next";

export const supportedLocales = ["cs", "en"] as const;
export type SupportedLocale = (typeof supportedLocales)[number];
export const defaultLocale: SupportedLocale = "cs";

const resources = {
  cs: {
    translation: {
      shell: {
        navigation: "Navigace organizace",
        events: "Akce",
        recipes: "Recepty",
        ingredients: "Suroviny",
        settings: "Nastavení organizace",
        language: "Jazyk",
        heading: "Plánování společného vaření",
        introduction:
          "Připravte akce, recepty a nákupy pro celou skupinu na jednom místě.",
        sectionPlaceholder:
          "Tato část bude dostupná v některém z dalších kroků.",
        userMenu: "Uživatelská nabídka",
        logout: "Odhlásit se",
        logoutError: "Odhlášení se nepodařilo. Zkuste to znovu.",
      },
      authentication: {
        loading: "Ověřujeme přihlášení…",
        startupError: "Přihlášení se nepodařilo načíst.",
        configurationError: "Přihlášení není správně nakonfigurováno.",
        retry: "Zkusit znovu",
      },
      developmentLogin: {
        title: "Vývojové přihlášení",
        warning:
          "Vývojová autentizace je aktivní. Používejte ji pouze pro místní vývoj a automatické testy.",
        introduction: "Vyberte předem připravenou testovací identitu.",
        identities: "Testovací identity",
        signInAs: "Přihlásit se jako {{name}}",
        unavailable: "Vývojová autentizace není k dispozici.",
        error: "Přihlášení se nepodařilo. Zkuste to znovu.",
      },
      googleLogin: {
        title: "Přihlášení do CookOps",
        introduction:
          "Pokračujte pomocí svého Google účtu s přiděleným přístupem.",
        loading: "Načítáme Google přihlášení…",
        loadError: "Google přihlášení se nepodařilo načíst.",
        signInError: "Přihlášení se nepodařilo. Zkuste to znovu.",
        retry: "Zkusit znovu",
      },
    },
  },
  en: {
    translation: {
      shell: {
        navigation: "Organization navigation",
        events: "Events",
        recipes: "Recipes",
        ingredients: "Ingredients",
        settings: "Organization settings",
        language: "Language",
        heading: "Plan group cooking",
        introduction:
          "Prepare events, recipes, and shopping for the whole group in one place.",
        sectionPlaceholder: "This area will become available in a later step.",
        userMenu: "User menu",
        logout: "Log out",
        logoutError: "Log out failed. Please try again.",
      },
      authentication: {
        loading: "Checking your sign-in…",
        startupError: "Your sign-in could not be loaded.",
        configurationError: "Sign-in is not configured correctly.",
        retry: "Try again",
      },
      developmentLogin: {
        title: "Development sign-in",
        warning:
          "Development authentication is active. Use it only for local development and automated tests.",
        introduction: "Select a pre-provisioned test identity.",
        identities: "Test identities",
        signInAs: "Sign in as {{name}}",
        unavailable: "Development authentication is unavailable.",
        error: "Sign-in failed. Please try again.",
      },
      googleLogin: {
        title: "Sign in to CookOps",
        introduction:
          "Continue with the Google account that has been granted access.",
        loading: "Loading Google sign-in…",
        loadError: "Google sign-in could not be loaded.",
        signInError: "Sign-in failed. Please try again.",
        retry: "Try again",
      },
    },
  },
} as const;

void i18n.use(initReactI18next).init({
  resources,
  lng: defaultLocale,
  fallbackLng: defaultLocale,
  supportedLngs: supportedLocales,
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
