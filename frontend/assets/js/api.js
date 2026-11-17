// api.js — Appels à l'API et connexion par mot de passe.

import { $, storage } from "./core.js";

let loginWaiters = null;

export async function api(path, { method = "GET", body, timeout = 120000 } = {}) {
  const headers = { "x-wsfs-token": storage.get("wsfs-token") || "" };
  if (body !== undefined) headers["content-type"] = "application/json";
  // Délai maximal : une requête perdue en route (proxy, disque saturé par
  // un empaquetage) ne doit pas laisser la page en attente sans fin
  let response;
  try {
    response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeout) });
  } catch (error) {
    throw new Error(error.name === "TimeoutError" ? `le serveur n'a pas répondu en ${Math.round(timeout / 60000)} min (réessayer)` : `connexion impossible : ${error.message}`);
  }
  if (response.status === 401 && path !== "/login") {
    await askLogin();
    return api(path, { method, body, timeout });
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 404 && data.detail === "Not Found") {
    // Route inconnue (pas une image inconnue) : le serveur sert une ancienne
    // version de l'API alors que la page, elle, est à jour.
    throw new Error("API introuvable : le serveur tourne sur une ancienne version, le redémarrer.");
  }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(" ; ") : data.detail;
    throw new Error(detail || `erreur ${response.status}`);
  }
  return data;
}

function askLogin() {
  if (!loginWaiters) {
    loginWaiters = [];
    $("login-err").hidden = true;
    $("dlg-login").showModal();
  }
  return new Promise((resolve) => loginWaiters.push(resolve));
}

export function init() {
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const { token } = await api("/login", { method: "POST", body: { password: $("login-pw").value } });
      storage.set("wsfs-token", token);
      $("dlg-login").close();
      const waiters = loginWaiters || [];
      loginWaiters = null;
      waiters.forEach((resolve) => resolve());
    } catch (error) {
      $("login-err").textContent = error.message;
      $("login-err").hidden = false;
    }
  });
  $("dlg-login").addEventListener("cancel", (event) => event.preventDefault());
}
