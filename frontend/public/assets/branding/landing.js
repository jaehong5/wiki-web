(() => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);

  // Access redirects are deliberately left unfollowed. Only the protected
  // endpoint's expected JSON response establishes an existing Wiki session.
  fetch('/api/status', {
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'manual',
    signal: controller.signal,
  }).then(async response => {
    if (!response.ok || response.type === 'opaqueredirect' || response.redirected) return;
    if (!response.headers.get('content-type')?.includes('application/json')) return;
    const status = await response.json();
    if (status && Number.isSafeInteger(status.notes) && status.notes >= 0) {
      location.replace('/app/');
    }
  }).catch(() => {
    // A missing, expired, or unavailable session keeps the public page usable.
  }).finally(() => clearTimeout(timeout));
})();
