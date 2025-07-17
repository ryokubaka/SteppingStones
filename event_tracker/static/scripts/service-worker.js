const CACHE_NAME = 'steppingstones-cache-v1';
const STATIC_ASSETS = [
  '/',
  '/static/css/bootstrap.min.css',
  '/static/css/bootstrap.min.css.map',
  '/static/scripts/bootstrap.bundle.min.js',
  '/static/scripts/bootstrap.bundle.min.js.map',
  '/static/scripts/jquery.min.js',
  '/static/scripts/moment.min.js',
  '/static/scripts/pdfmake.min.js',
  '/static/scripts/datetime-moment.min.js',
  '/static/fonts/exo/exo.css',
  '/static/fonts/exo/Exo-200.woff2',
  '/static/fonts/exo/Exo-400.woff2',
  '/static/fonts/exo/Exo-200.ttf',
  '/static/fonts/exo/Exo-400.ttf',
  '/static/fontawesomefree/css/fontawesome.min.css',
  '/static/fontawesomefree/css/regular.min.css',
  '/static/fontawesomefree/css/solid.min.css',
  '/static/css/event_table.css',
  '/static/scripts/event_table.js',
  '/static/scripts/jquery.formset.js',
  '/static/scripts/jquery.expander.js',
  '/static/scripts/bootstrap_input.js',
  '/static/scripts/safe-nonce.min.js',
  '/static/scripts/ss-forms.js',
  '/static/scripts/maintainscroll.min.js',
  '/static/fonts/RobotoMono-Regular.ttf',
  // Add more static assets as needed
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_ASSETS);
    })
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
      );
    })
  );
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  
  // Network first strategy with cache fallback
  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Cache successful responses
        if (response.status === 200) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then(cache => {
            cache.put(event.request, responseClone);
          });
        }
        return response;
      })
      .catch(() => {
        // Fallback to cache if network fails
        return caches.match(event.request).then(res => {
          if (res) {
            return res;
          }
          // If not in cache, try to serve the homepage
          return caches.match('/');
        });
      })
  );
}); 