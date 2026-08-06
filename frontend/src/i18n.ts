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
