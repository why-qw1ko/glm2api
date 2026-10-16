"use strict";
(() => {
  const preference = window.matchMedia("(prefers-color-scheme: dark)");
  let selected = null;
  try { selected = localStorage.getItem("glm-panel-theme"); } catch {}
  const apply = theme => {
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll("[data-theme-toggle]").forEach(button => {
      const dark = theme === "dark";
      const label = dark ? "切换浅色主题" : "切换深色主题";
      button.title = label;
      button.setAttribute("aria-label", label);
      button.setAttribute("aria-pressed", String(dark));
      const icon = button.querySelector(".fluent-icon");
      if (icon) icon.className = `fluent-icon icon-${dark ? "weather_sunny" : "weather_moon"}`;
    });
  };
  const initial = selected === "dark" || selected === "light" ? selected : preference.matches ? "dark" : "light";
  apply(initial);
  document.addEventListener("DOMContentLoaded", () => {
    apply(document.documentElement.dataset.theme);
    document.querySelectorAll("[data-theme-toggle]").forEach(button => {
      button.addEventListener("click", () => {
        selected = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        try { localStorage.setItem("glm-panel-theme", selected); } catch {}
        apply(selected);
      });
    });
  });
  preference.addEventListener("change", event => {
    if (selected !== "dark" && selected !== "light") apply(event.matches ? "dark" : "light");
  });
})();
