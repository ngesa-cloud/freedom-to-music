// b33t Service Worker — Network-First & Cache-Busting Engine (v4)
const CACHE_NAME = 'b33t-shell-v4';
const OFFLINE_AUDIO_CACHE = 'b33t-offline-audio';

const PRECACHE_ASSETS = [
  '/',
  '/manifest.json',
  '/icon.png'
];

// 1. Install Phase: Cache shell assets and force immediate activation
self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(PRECACHE_ASSETS).catch((err) => {
        console.warn('[SW v4] Pre-caching notice:', err);
      });
    })
  );
});

// 2. Activate Phase: Purge all stale caches from previous builds
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME && key !== OFFLINE_AUDIO_CACHE) {
            console.log('[SW v4] Deleting obsolete cache:', key);
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// 3. Fetch Phase: Network-First for UI & HTML, Cache-First only for offline audio fallback
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // A. Offline Audio Streams: Check network, then fallback to offline IndexedDB / audio cache
  if (url.pathname.startsWith('/api/stream/')) {
    event.respondWith(
      fetch(event.request).catch(async () => {
        const cache = await caches.open(OFFLINE_AUDIO_CACHE);
        const cached = await cache.match(event.request);
        if (cached) return cached;
        throw new Error("Offline audio stream not found");
      })
    );
    return;
  }

  // B. Bypass Service Worker entirely for dynamic API queries and APK binary downloads
  if (url.pathname.startsWith('/api/') || 
      url.pathname.endsWith('.apk') || 
      event.request.method !== 'GET') {
    return;
  }

  // C. Network-First Strategy for HTML & Assets: Always fetch latest from server, fall back if offline
  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const clone = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, clone);
          });
        }
        return networkResponse;
      })
      .catch(async () => {
        const cachedResponse = await caches.match(event.request);
        if (cachedResponse) return cachedResponse;
        if (event.request.mode === 'navigate') {
          return caches.match('/');
        }
      })
  );
});
