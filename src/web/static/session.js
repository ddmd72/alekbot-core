/* Shared session handling for the Cabinet and the voice call pages (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.4).

   The access cookie lives 15 minutes; the refresh cookie renews it. A page must never send the user to a Google
   login because the access cookie happened to expire: it asks /auth/refresh first, and only when the server refuses
   (a non-2xx answer) is the session really over. A refresh that never reached the server (offline, a dropped
   connection) ends nothing: the request that needed it fails as it would offline, and the next request or the next
   return to the page tries again. One refresh runs at a time per page, so several requests failing together share
   one rotation of the refresh secret instead of racing each other.

   Passkeys (§7.3): ensurePasskey() is the one helper a call start runs before it starts. It asks the server whether
   this session needs a fresh passkey assertion (a user with a passkey, none in the last 5 minutes), and if so runs
   the system passkey sheet and hands the assertion back; it resolves true when the call may start. addPasskey()
   enrols a passkey from this session ("Add Face ID"). WebAuthn speaks ArrayBuffers and the server JSON with
   base64url, so both carry the conversion.

   Exposes window.alekSession = { authFetch, ensureSession, ensurePasskey, addPasskey }. Served anonymously at
   /auth/session.js. */
(function () {
  "use strict";

  var EARLY_REFRESH_SECONDS = 120;
  var refreshInFlight = null;
  var ensureInFlight = null;

  function is2xx(resp) {
    return resp.status >= 200 && resp.status < 300;
  }

  function goToLogin() {
    var next = location.pathname + location.search;
    location.replace("/auth/login?next=" + encodeURIComponent(next));
  }

  // A page that is leaving for the login must not run its own error handling meanwhile.
  function never() {
    return new Promise(function () {});
  }

  // Resolves "ok" when the cookies were renewed, "refused" when the server answered anything else (the session is
  // over), "offline" when no answer came (nothing is decided; a later request tries again).
  function refresh() {
    if (!refreshInFlight) {
      refreshInFlight = fetch("/auth/refresh", { method: "POST", credentials: "same-origin" })
        .then(function (resp) { return is2xx(resp) ? "ok" : "refused"; })
        .catch(function () { return "offline"; })
        .then(function (outcome) {
          refreshInFlight = null;
          return outcome;
        });
    }
    return refreshInFlight;
  }

  function authFetch(url, opts) {
    var init = Object.assign({ credentials: "same-origin" }, opts || {});
    return fetch(url, init).then(function (resp) {
      if (resp.status !== 401) return resp;
      return refresh().then(function (outcome) {
        if (outcome === "offline") return resp;
        if (outcome !== "ok") {
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

  function refreshOrLogin() {
    return refresh().then(function (outcome) {
      if (outcome === "refused") goToLogin();
    });
  }

  function checkSession() {
    return fetch("/auth/me", { credentials: "same-origin" })
      .then(function (resp) {
        if (resp.status === 401) return refreshOrLogin();
        if (resp.status !== 200) return undefined;
        return resp.json().then(function (body) {
          var left = body && typeof body.expires_at === "number" ? body.expires_at - Date.now() / 1000 : null;
          if (left !== null && left < EARLY_REFRESH_SECONDS) return refreshOrLogin();
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

  /* ── Passkeys ─────────────────────────────────────────── */

  function fromB64u(text) {
    var b64 = text.replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    var raw = atob(b64);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes.buffer;
  }

  function toB64u(buffer) {
    var bytes = new Uint8Array(buffer);
    var raw = "";
    for (var i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function withIds(list) {
    return (list || []).map(function (c) { return Object.assign({}, c, { id: fromB64u(c.id) }); });
  }

  function requestOptions(o) {
    return Object.assign({}, o, { challenge: fromB64u(o.challenge), allowCredentials: withIds(o.allowCredentials) });
  }

  function creationOptions(o) {
    return Object.assign({}, o, {
      challenge: fromB64u(o.challenge),
      user: Object.assign({}, o.user, { id: fromB64u(o.user.id) }),
      excludeCredentials: withIds(o.excludeCredentials),
    });
  }

  function credentialJSON(cred) {
    var r = cred.response;
    var response = { clientDataJSON: toB64u(r.clientDataJSON) };
    if (r.attestationObject) {
      response.attestationObject = toB64u(r.attestationObject);
      response.transports = typeof r.getTransports === "function" ? r.getTransports() : [];
    } else {
      response.authenticatorData = toB64u(r.authenticatorData);
      response.signature = toB64u(r.signature);
      if (r.userHandle) response.userHandle = toB64u(r.userHandle);
    }
    return { id: cred.id, rawId: toB64u(cred.rawId), type: cred.type, response: response,
             clientExtensionResults: {}, authenticatorAttachment: cred.authenticatorAttachment || null };
  }

  function postJSON(url, body) {
    return authFetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(body || {}) });
  }

  function supported() {
    return typeof window.PublicKeyCredential !== "undefined" && !!(navigator.credentials && navigator.credentials.get);
  }

  // Resolves true when a call may start: no fresh passkey is needed, or the sheet just gave one the server took.
  // False when it was cancelled, refused, or could not run (the server refuses the start in that case too).
  function ensurePasskey() {
    return postJSON("/api/user/passkeys/step-up/options")
      .then(function (resp) { return is2xx(resp) ? resp.json() : null; })
      .then(function (body) {
        if (!body) return false;
        if (!body.required) return true;
        if (!supported()) return false;
        return navigator.credentials.get({ publicKey: requestOptions(body.options) })
          .then(function (cred) { return postJSON("/api/user/passkeys/step-up/verify", { credential: credentialJSON(cred) }); })
          .then(is2xx);
      })
      .catch(function () { return false; });
  }

  // Enrols a passkey from this session. Resolves "added", "exists" (this keychain already holds one of the user's
  // passkeys), "cancelled" or "failed".
  function addPasskey() {
    if (!supported()) return Promise.resolve("failed");
    return postJSON("/api/user/passkeys/register/options")
      .then(function (resp) { return is2xx(resp) ? resp.json() : null; })
      .then(function (body) {
        if (!body) return "failed";
        return navigator.credentials.create({ publicKey: creationOptions(body.options) })
          .then(function (cred) { return postJSON("/api/user/passkeys/register/verify", { credential: credentialJSON(cred) }); })
          .then(function (resp) { return is2xx(resp) ? "added" : "failed"; });
      })
      .catch(function (e) {
        if (e && e.name === "InvalidStateError") return "exists";
        if (e && e.name === "NotAllowedError") return "cancelled";
        return "failed";
      });
  }

  window.alekSession = { authFetch: authFetch, ensureSession: ensureSession, ensurePasskey: ensurePasskey,
                         addPasskey: addPasskey };
})();
