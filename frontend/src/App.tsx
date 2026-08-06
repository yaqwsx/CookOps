import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import type { SupportedLocale } from "./i18n";
import "./app.css";

const sections = ["events", "recipes", "ingredients", "settings"] as const;

export function App() {
  const { i18n, t } = useTranslation();
  const locale = (i18n.resolvedLanguage ?? "cs") as SupportedLocale;

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  return (
    <div className="app-shell">
      <header className="app-header">
        <a className="brand" href="#main">
          CookOps
        </a>

        <nav aria-label={t("shell.navigation")}>
          <ul className="primary-navigation">
            {sections.map((section) => (
              <li key={section}>
                <a href={`#${section}`}>{t(`shell.${section}`)}</a>
              </li>
            ))}
          </ul>
        </nav>

        <label className="locale-picker">
          <span>{t("shell.language")}</span>
          <select
            value={locale}
            onChange={(event) => void i18n.changeLanguage(event.target.value)}
          >
            <option value="cs">Čeština</option>
            <option value="en">English</option>
          </select>
        </label>
      </header>

      <main id="main" className="app-main">
        <section className="introduction" aria-labelledby="app-heading">
          <p className="eyebrow">CookOps</p>
          <h1 id="app-heading">{t("shell.heading")}</h1>
          <p>{t("shell.introduction")}</p>
        </section>

        <div className="section-grid">
          {sections.map((section) => (
            <section id={section} className="section-card" key={section}>
              <h2>{t(`shell.${section}`)}</h2>
              <p>{t("shell.sectionPlaceholder")}</p>
            </section>
          ))}
        </div>
      </main>
    </div>
  );
}
