const CACHE_NAME = 'jarvis-os-cache-v1';
const ASSETS_TO_CACHE = [
  '/',
  '/static/icon-512.png',
  '/static/sounds/processing.mp3',
  '/static/sounds/wake_word.mp3',
  '/static/sounds/error.mp3'
];

// 서비스 워커 설치 및 캐싱
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[Service Worker] Caching app shell');
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
  self.skipWaiting();
});

// 서비스 워커 활성화 및 캐시 정리
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cache) => {
          if (cache !== CACHE_NAME) {
            console.log('[Service Worker] Clearing old cache', cache);
            return caches.delete(cache);
          }
        })
      );
    })
  );
  self.clients.claim();
});

// 캐시 우선 정책 및 네트워크 폴백
self.addEventListener('fetch', (event) => {
  // API 및 동적 요청 등은 캐시하지 않고 네트워크로 바로 전송
  if (
    event.request.url.includes('/api/') || 
    event.request.url.includes('/ai/') || 
    event.request.url.includes('/calendar/') || 
    event.request.url.includes('/gmail/') ||
    event.request.method !== 'GET'
  ) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }
      return fetch(event.request).then((networkResponse) => {
        // 성공적인 GET 요청에 대해서만 동적 캐싱 진행
        if (networkResponse && networkResponse.status === 200 && networkResponse.type === 'basic') {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseToCache);
          });
        }
        return networkResponse;
      }).catch(() => {
        // 오프라인 상태에서 HTML 요청인 경우 루트(/) 리턴
        if (event.request.headers.get('accept').includes('text/html')) {
          return caches.match('/');
        }
      });
    })
  );
});
