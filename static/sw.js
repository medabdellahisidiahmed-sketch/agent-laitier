// Service worker minimal : nécessaire pour que le navigateur propose
// "Ajouter à l'écran d'accueil". Pas de mise en cache complexe pour l'instant.
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Laisse passer toutes les requêtes normalement (pas de mode hors-ligne pour l'instant)
  event.respondWith(fetch(event.request));
});
