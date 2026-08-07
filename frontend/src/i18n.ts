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
        organization: "Organizace",
        organizationsLoading: "Načítáme organizace…",
        organizationsError: "Organizace se nepodařilo načíst. Zkuste to znovu.",
        noOrganizations: "Nemáte přístup k žádné aktivní organizaci.",
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
        scope: "Přehled čte uložené projekce akcí a čekající místní změny.",
        offline: "Zobrazujeme uložené akce bez připojení.",
        error: "Akce se nepodařilo načíst. Zkuste to znovu.",
        retry: "Zkusit znovu",
        empty: "V této organizaci zatím nejsou žádné akce.",
        more: "Načíst další akce",
        dateRange: "{{start}} až {{end}}",
        attendance: "Očekávaná účast",
        budget: "Rozpočet",
        open: "Otevřít plán",
        lifecycle: { active: "Aktivní", archived: "Archivovaná" },
      },
      eventsCreate: {
        heading: "Nová akce",
        name: "Název",
        startDate: "Začátek",
        endDate: "Konec",
        attendance: "Očekávaná účast",
        budget: "Rozpočet",
        location: "Místo",
        note: "Poznámka",
        submit: "Uložit akci",
        saved: "Akce je uložena místně a bude synchronizována.",
        errors: {
          name: "Zadejte název do 200 znaků.",
          startDate: "Zadejte platné datum začátku.",
          endDate: "Zadejte platné datum konce.",
          dateRange:
            "Konec nesmí předcházet začátku a akce může trvat nejvýše 366 dní.",
          attendance: "Účast musí být nezáporné celé číslo.",
          budget: "Rozpočet musí být nezáporné desetinné číslo.",
          location: "Místo může mít nejvýše 300 znaků.",
          organizationCurrency: "Nejdřív se musí načíst měna organizace.",
          unavailable: "Akci se nepodařilo uložit místně. Zkuste to znovu.",
        },
      },
      eventsEdit: {
        attendance: "Očekávaná účast",
        submit: "Uložit účast",
        saved: "Účast je uložena místně a bude synchronizována.",
        errors: {
          attendance: "Účast musí být nezáporné celé číslo.",
          event: "Akci nelze místně upravit.",
          unavailable: "Účast se nepodařilo uložit místně. Zkuste to znovu.",
        },
      },
      planner: {
        heading: "Plán akce",
        loading: "Načítáme plán akce…",
        unavailable: "Plán akce není uložený v tomto zařízení.",
        error: "Plán se nepodařilo načíst. Zkuste to znovu.",
        offline: "Zobrazujeme uložený plán bez připojení.",
        archived: "Tato akce je archivovaná a plán je jen pro čtení.",
        dateRange: "{{start}} až {{end}}",
        attendance: "Očekávaná účast",
        lifecycle: "Stav",
        addHeading: "Přidat recept",
        day: "Den",
        role: "Chod",
        recipe: "Recept",
        add: "Přidat do plánu",
        saved: "Recept je uložen místně a bude synchronizován.",
        noAddOptions:
          "Pro přidání receptu je potřeba uložený den, chod a recept.",
        emptyRole: "Pro tento chod zatím není naplánovaný recept.",
        diners: "Strávníci: {{count}}",
        errors: {
          unavailable: "Recept se nepodařilo uložit místně. Zkuste to znovu.",
        },
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
        organization: "Organization",
        organizationsLoading: "Loading organizations…",
        organizationsError:
          "Organizations could not be loaded. Please try again.",
        noOrganizations: "You do not have access to an active organization.",
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
        scope:
          "This overview reads stored event projections and pending local changes.",
        offline: "Showing stored events while offline.",
        error: "Events could not be loaded. Please try again.",
        retry: "Try again",
        empty: "There are no events in this organization yet.",
        more: "Load more events",
        dateRange: "{{start}} to {{end}}",
        attendance: "Expected attendance",
        budget: "Budget",
        open: "Open planner",
        lifecycle: { active: "Active", archived: "Archived" },
      },
      eventsCreate: {
        heading: "New event",
        name: "Name",
        startDate: "Start date",
        endDate: "End date",
        attendance: "Expected attendance",
        budget: "Budget",
        location: "Location",
        note: "Note",
        submit: "Save event",
        saved: "The event is saved locally and will synchronize.",
        errors: {
          name: "Enter a name of at most 200 characters.",
          startDate: "Enter a valid start date.",
          endDate: "Enter a valid end date.",
          dateRange:
            "The end cannot precede the start and an event can last at most 366 days.",
          attendance: "Attendance must be a non-negative whole number.",
          budget: "Budget must be a non-negative decimal number.",
          location: "Location must be at most 300 characters.",
          organizationCurrency: "Load the organization currency first.",
          unavailable:
            "The event could not be saved locally. Please try again.",
        },
      },
      eventsEdit: {
        attendance: "Expected attendance",
        submit: "Save attendance",
        saved: "Attendance is saved locally and will synchronize.",
        errors: {
          attendance: "Attendance must be a non-negative whole number.",
          event: "The event cannot be edited locally.",
          unavailable:
            "Attendance could not be saved locally. Please try again.",
        },
      },
      planner: {
        heading: "Event planner",
        loading: "Loading the event planner…",
        unavailable: "This event planner is not stored on this device.",
        error: "The planner could not be loaded. Please try again.",
        offline: "Showing the stored planner while offline.",
        archived: "This event is archived and the planner is read-only.",
        dateRange: "{{start}} to {{end}}",
        attendance: "Expected attendance",
        lifecycle: "Lifecycle",
        addHeading: "Add recipe",
        day: "Day",
        role: "Meal role",
        recipe: "Recipe",
        add: "Add to planner",
        saved: "The recipe is saved locally and will synchronize.",
        noAddOptions:
          "A stored day, meal role, and recipe are required to add a recipe.",
        emptyRole: "No recipe is scheduled for this meal role yet.",
        diners: "Diners: {{count}}",
        errors: {
          unavailable:
            "The recipe could not be saved locally. Please try again.",
        },
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
