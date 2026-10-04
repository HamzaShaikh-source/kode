// Applied before first paint (loaded synchronously in <head>) so there is no light/dark flash. External file so a strict CSP works.
(function () {
  try {
    var t = localStorage.getItem('kode.theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = t;
  } catch (e) { /* storage blocked: default light */ }
})();
