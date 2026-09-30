/* AI Business Platform — JS ligero, sin dependencias. */

(function () {
  "use strict";

  // ---------- Login ----------
  var loginForm = document.getElementById("login-form");
  if (loginForm) {
    var alertBox = document.getElementById("login-alert");
    var submitBtn = document.getElementById("login-submit");

    loginForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var tenant = document.getElementById("tenant").value.trim();
      var username = document.getElementById("username").value.trim();
      var password = document.getElementById("password").value;

      alertBox.classList.remove("show");
      submitBtn.disabled = true;
      submitBtn.textContent = "Ingresando...";

      fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ tenant: tenant, username: username, password: password }),
      })
        .then(function (res) {
          return res.json().then(function (data) { return { ok: res.ok, data: data }; });
        })
        .then(function (result) {
          if (!result.ok) {
            throw new Error(result.data && result.data.error ? result.data.error : "No se pudo iniciar sesion");
          }
          var params = new URLSearchParams(window.location.search);
          var next = params.get("next");
          window.location.href = next && next.startsWith("/") ? next : "/dashboard";
        })
        .catch(function (err) {
          alertBox.textContent = err.message;
          alertBox.classList.add("show");
          submitBtn.disabled = false;
          submitBtn.textContent = "Ingresar";
        });
    });
  }

  // ---------- Logout ----------
  document.querySelectorAll("[data-logout]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" })
        .finally(function () { window.location.href = "/login"; });
    });
  });

  // ---------- Sidebar movil ----------
  var menuToggle = document.querySelector("[data-menu-toggle]");
  var overlay = document.querySelector("[data-sidebar-overlay]");
  function closeSidebar() { document.body.classList.remove("sidebar-open"); }
  if (menuToggle) {
    menuToggle.addEventListener("click", function () {
      document.body.classList.toggle("sidebar-open");
    });
  }
  if (overlay) overlay.addEventListener("click", closeSidebar);

  // Cerrar sidebar movil al navegar
  document.querySelectorAll(".sidebar a.nav-item").forEach(function (a) {
    a.addEventListener("click", closeSidebar);
  });
})();
