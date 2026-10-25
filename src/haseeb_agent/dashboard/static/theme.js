// Runs before first paint (a plain blocking script, no data): set data-theme on <html> so the page never flashes the
// wrong theme. The choice is "midnight" or "daylight"; a stored toggle wins, otherwise prefers-color-scheme decides.
(function () {
  "use strict";
  var stored = null;
  try { stored = window.localStorage.getItem("ha_theme"); } catch (e) { stored = null; }
  var dark = true;
  try { dark = !window.matchMedia("(prefers-color-scheme: light)").matches; } catch (e) { dark = true; }
  var theme = stored === "midnight" || stored === "daylight" ? stored : (dark ? "midnight" : "daylight");
  document.documentElement.setAttribute("data-theme", theme);
})();
