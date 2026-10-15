// Service worker : (1) permet "Ajouter à l'écran d'accueil", (2) reçoit les
// notifications push (nouvelle commande) même quand l'application est fermée.
self.addEventListener("install", () => { self.skipWaiting(); });
self.addEventListener("activate", (event) => { event.waitUntil(self.clients.claim()); });

self.addEventListener("fetch", (event) => {
  // Laisse passer toutes les requêtes normalement (pas de mode hors-ligne pour l'instant)
  event.respondWith(fetch(event.request));
});

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { titre: "Nouvelle notification", corps: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(d.titre || "Nouvelle commande", {
    body: d.corps || "",
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    tag: "commande-" + (d.id || Date.now()),
    renotify: true,                    // sonne à chaque nouveau message, même avec le même repère
    requireInteraction: true,          // reste affichée jusqu'à ce que vous la touchiez (Android, ordinateur)
    silent: false,                     // demande explicitement le son et la vibration de la notification
    vibrate: [400, 150, 400, 150, 400, 150, 800],
    data: { url: "/admin" }
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/admin";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((fenetres) => {
    for (const f of fenetres) { if (f.url.indexOf("/admin") !== -1 && "focus" in f) return f.focus(); }
    return self.clients.openWindow(url);
  }));
});
