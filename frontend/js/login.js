import { api } from "./api.js";

// Already logged in? Skip straight to the dashboard.
api.me().then(() => { window.location.href = "/dashboard.html"; }).catch(() => {});

const form = document.getElementById("login-form");
const errorEl = document.getElementById("login-error");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.hidden = true;
  try {
    await api.login(form.username.value, form.password.value);
    window.location.href = "/dashboard.html";
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.hidden = false;
  }
});
