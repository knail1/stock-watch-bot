// Confirm destructive actions and auto-refresh the watches list while visible.
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
(function () {
  var el = document.querySelector("[data-autorefresh]");
  if (!el) return;
  var secs = parseInt(el.getAttribute("data-autorefresh"), 10) || 30;
  setInterval(function () {
    if (document.visibilityState === "visible") window.location.reload();
  }, secs * 1000);
})();
