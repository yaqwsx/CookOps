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
      eventsOverview: {
        loading: "Načítáme akce…",
        loadingMore: "Načítáme další akce…",
        scope:
          "Tento přehled je pouze pro čtení a načítá se online; nejde o offline projekci.",
        error: "Akce se nepodařilo načíst. Zkuste to znovu.",
        retry: "Zkusit znovu",
        empty: "V této organizaci zatím nejsou žádné akce.",
        more: "Načíst další akce",
        dateRange: "{{start}} až {{end}}",
        attendance: "Očekávaná účast",
        budget: "Rozpočet",
        lifecycle: { active: "Aktivní", archived: "Archivovaná" },
      },
      synchronization: {
        caughtUp: "Synchronizováno",
        offline: "Bez připojení",
        pending: "Čekají změny: {{count}}",
        syncing: "Synchronizujeme…",
        retrying: "Synchronizace bude zopakována",
        failed: "Změny vyžadují pozornost: {{count}}",
        storageUnavailable: "Místní úložiště synchronizace není k dispozici.",
        pendingUploads: "Čeká nahrání fotografií: {{count}}",
        clockSkew: "Čas zařízení se liší od serveru.",
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
      eventsOverview: {
        loading: "Loading events…",
        loadingMore: "Loading more events…",
        scope:
          "This read-only overview is loaded online; it is not an offline projection.",
        error: "Events could not be loaded. Please try again.",
        retry: "Try again",
        empty: "There are no events in this organization yet.",
        more: "Load more events",
        dateRange: "{{start}} to {{end}}",
        attendance: "Expected attendance",
        budget: "Budget",
        lifecycle: { active: "Active", archived: "Archived" },
      },
      synchronization: {
        caughtUp: "Synchronized",
        offline: "Offline",
        pending: "Pending changes: {{count}}",
        syncing: "Synchronizing…",
        retrying: "Synchronization will retry",
        failed: "Changes need attention: {{count}}",
        storageUnavailable: "Local synchronization storage is unavailable.",
        pendingUploads: "Pending photo uploads: {{count}}",
        clockSkew: "Your device time differs from the server.",
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
