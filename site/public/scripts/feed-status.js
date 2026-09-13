// Periodically re-checks the feed's meta.json so a visitor who leaves the
// tab open sees a stale-feed warning without reloading. Best-effort only:
// any network failure is swallowed and the page keeps showing whatever it
// rendered at build time. Never throws, never blocks the rest of the page.
(function () {
  const STALE_THRESHOLD_MS = 6 * 60 * 60 * 1000;
  const POLL_INTERVAL_MS = 5 * 60 * 1000;

  const banner = document.getElementById('feed-status-banner');
  const text = document.getElementById('feed-status-text');
  if (!banner) return;

  const baseUrl = banner.dataset.feedBaseUrl;
  if (!baseUrl) return; // local/sample mode: nothing to poll

  function showStale(generatedAt) {
    if (!text) return;
    banner.classList.remove('hidden');
    banner.classList.add('border-red-200', 'bg-red-50', 'text-red-800');
    banner.hidden = false;
    text.textContent = `Our last successful check was ${new Date(generatedAt).toLocaleString()}. Prices below may be out of date.`;
  }

  function showError() {
    if (!text) return;
    banner.classList.remove('hidden');
    banner.classList.add('border-red-200', 'bg-red-50', 'text-red-800');
    banner.hidden = false;
    text.textContent = "We couldn't reach our price feed just now. Showing the last deals we successfully loaded.";
  }

  async function check() {
    try {
      const res = await fetch(`${baseUrl}/meta.json`, { cache: 'no-store' });
      if (!res.ok) {
        showError();
        return;
      }
      const meta = await res.json();
      const generatedAt = new Date(meta.generated_at).getTime();
      if (Number.isNaN(generatedAt)) {
        showError();
        return;
      }
      if (Date.now() - generatedAt > STALE_THRESHOLD_MS) {
        showStale(meta.generated_at);
      }
    } catch {
      showError();
    }
  }

  window.setInterval(check, POLL_INTERVAL_MS);
})();
