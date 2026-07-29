(() => {
  const ONLINE_INTERVAL_MS = 30000;
  const OFFLINE_INTERVAL_MS = 5000;
  const PROBE_TIMEOUT_MS = 5000;

  let online = navigator.onLine;
  let started = false;
  let timer = null;
  let probePromise = null;

  function emit(reason) {
    window.dispatchEvent(new CustomEvent('circlecore:connectivity', {
      detail: {online, reason},
    }));
  }

  function setOnline(nextOnline, reason) {
    const changed = online !== nextOnline;
    online = nextOnline;
    if (changed || reason === 'initial') emit(reason);
    schedule();
    return online;
  }

  async function probe(reason = 'probe') {
    if (probePromise) return probePromise;
    if (!navigator.onLine) return setOnline(false, 'browser-offline');

    probePromise = (async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
      try {
        const response = await fetch(`/healthz/?connectivity=${Date.now()}`, {
          cache: 'no-store',
          credentials: 'same-origin',
          headers: {'X-Circle-Core-Connectivity': '1'},
          signal: controller.signal,
        });
        return setOnline(response.ok, response.ok ? reason : 'server-unavailable');
      } catch (_error) {
        return setOnline(false, 'network-failure');
      } finally {
        clearTimeout(timeout);
        probePromise = null;
      }
    })();
    return probePromise;
  }

  function schedule() {
    clearTimeout(timer);
    if (!started || document.visibilityState === 'hidden') return;
    timer = setTimeout(
      () => probe('scheduled'),
      online ? ONLINE_INTERVAL_MS : OFFLINE_INTERVAL_MS,
    );
  }

  function start() {
    if (started) return;
    started = true;
    window.addEventListener('online', () => probe('browser-online'));
    window.addEventListener('offline', () => setOnline(false, 'browser-offline'));
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') probe('visible');
      else schedule();
    });
    emit('initial');
    probe('initial');
  }

  window.CircleCoreConnectivity = {
    isOnline: () => online,
    probe,
    start,
  };

  start();
})();
