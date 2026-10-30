import { api } from "./api.js";
import { applyAccountPreferences, forgetSession } from "./preferences.js";

// Already logged in? Skip straight to the dashboard. Otherwise forget the
// last session's interface copy (it may have ended without a logout).
api.me().then(() => { window.location.href = "/dashboard.html"; }).catch(() => forgetSession());

const form = document.getElementById("login-form");
const errorEl = document.getElementById("login-error");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.hidden = true;
  try {
    const user = await api.login(form.username.value, form.password.value);
    applyAccountPreferences(user.preferences);       // the dashboard opens in the user's own theme
    window.location.href = "/dashboard.html";
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.hidden = false;
  }
});
