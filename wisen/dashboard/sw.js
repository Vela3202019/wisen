// wisen service worker: shows Web Push notifications and opens the dashboard
// at the page a notification points to. Nothing is cached: the dashboard
// always reads live data from the server.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  event.waitUntil(self.registration.showNotification(data.title || "wisen", {
    body: data.body || "",
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    tag: data.url || "wisen",
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  // Only ever open a page on this origin, whatever the payload says.
  const url = new URL(event.notification.data?.url || "/", self.location.origin);
  const target = url.origin === self.location.origin ? url.href : self.location.origin + "/";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(windows => {
    for (const w of windows) {
      if (w.url.startsWith(self.location.origin)) return w.navigate(target).then(c => (c || w).focus());
    }
    return self.clients.openWindow(target);
  }));
});
