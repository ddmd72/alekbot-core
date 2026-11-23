/* Shared session handling for the Cabinet and the voice call pages (CABINET_DEVICE_SESSIONS_RFC §7.4).

   The access cookie lives 15 minutes; the refresh cookie renews it. A page must never send the user to a Google
   login because the access cookie happened to expire: it asks /auth/refresh first, and only when that fails is the
   session really over. One refresh runs at a time per page, so several requests failing together share one rotation
   of the refresh secret instead of racing each other.

   Exposes window.alekSession = { authFetch, ensureSession }. Served anonymously at /auth/session.js. */
(function () {
  "use strict";

  var EARLY_REFRESH_SECONDS = 120;
  var refreshInFlight = null;
  var ensureInFlight = null;

  function goToLogin() {
    var next = location.pathname + location.search;
    location.replace("/auth/login?next=" + encodeURIComponent(next));
  }

  // A page that is leaving for the login must not run its own error handling meanwhile.
  function never() {
    return new Promise(function () {});
  }

  // Resolves true when the cookies were renewed. Any other outcome (non-200, network failure) is "session over".
  function refresh() {
    if (!refreshInFlight) {
      refreshInFlight = fetch("/auth/refresh", { method: "POST", credentials: "same-origin" })
        .then(function (resp) { return resp.status === 200; })
        .catch(function () { return false; })
        .then(function (ok) {
          refreshInFlight = null;
          return ok;
        });
    }
    return refreshInFlight;
  }

  function authFetch(url, opts) {
    var init = Object.assign({ credentials: "same-origin" }, opts || {});
    return fetch(url, init).then(function (resp) {
      if (resp.status !== 401) return resp;
      return refresh().then(function (ok) {
        if (!ok) {
          goToLogin();
          return never();
        }
        return fetch(url, init).then(function (retry) {
          if (retry.status === 401) {
            goToLogin();
            return never();
          }
          return retry;
        });
      });
    });
  }

  function checkSession() {
    return fetch("/auth/me", { credentials: "same-origin" })
      .then(function (resp) {
        if (resp.status === 401) {
          return refresh().then(function (ok) {
            if (!ok) goToLogin();
          });
        }
        if (resp.status !== 200) return undefined;
        return resp.json().then(function (body) {
          var left = body && typeof body.expires_at === "number" ? body.expires_at - Date.now() / 1000 : null;
          if (left !== null && left < EARLY_REFRESH_SECONDS) {
            return refresh().then(function (ok) {
              if (!ok) goToLogin();
            });
          }
          return undefined;
        });
      })
      .catch(function () {
        // Offline or transient: the next request surfaces the error itself.
      });
  }

  function ensureSession() {
    if (!ensureInFlight) {
      ensureInFlight = checkSession().then(function () {
        ensureInFlight = null;
      });
    }
    return ensureInFlight;
  }

  // A Home Screen app returning from the background refreshes before the user taps anything.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") ensureSession();
  });

  window.alekSession = { authFetch: authFetch, ensureSession: ensureSession };
})();
