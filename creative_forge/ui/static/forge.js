/* Creative Forge: gallery players. Videos are served with native controls (they work without this file);
   here the controls are hidden behind one play button until the card is played, so a grid of posters stays calm. */
(function () {
  document.querySelectorAll('video[data-quiet]').forEach(function (v) {
    var box = v.parentNode, btn = document.createElement('button');
    v.controls = false;
    btn.type = 'button';
    btn.className = 'cr-play';
    btn.setAttribute('aria-label', v.dataset.quiet || 'Play');
    btn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7.5-4.5z" fill="currentColor"/></svg>';
    btn.addEventListener('click', function () {
      document.querySelectorAll('video[data-quiet]').forEach(function (o) { if (o !== v && !o.paused) o.pause(); });
      v.controls = true; box.classList.add('playing'); v.play();
    });
    box.appendChild(btn);
  });
})();
/* Confirmations and auto-submitting filters, in place of inline onsubmit / onchange handlers (the
   Content-Security-Policy runs no inline script). */
(function () {
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (form && form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) ev.preventDefault();
  });
  document.addEventListener('change', function (ev) {
    var el = ev.target;
    if (el && el.hasAttribute && el.hasAttribute('data-autosubmit') && el.form) el.form.submit();
  });
})();
/* An htmx action that fails says so. The server refused it (403: the page's token is from before a restart of the
   app) or never got it (the app is closed); without this the button would seem to do nothing. The sentences come
   from data-* attributes on <body>, in the page's language. A form answered in place with an error marks the
   message data-focus, so a long page scrolls to it. */
(function () {
  var body = document.body;
  function toast(text) {
    var box = document.getElementById('forge-toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'forge-toast';
      box.className = 'toast';
      box.setAttribute('role', 'alert');
      document.body.appendChild(box);
    }
    box.textContent = '';
    var p = document.createElement('p');
    p.textContent = text;
    var row = document.createElement('div');
    row.className = 'row';
    var reload = document.createElement('button');
    reload.type = 'button';
    reload.className = 'btn sm primary';
    reload.textContent = body.dataset.reload || 'Reload';
    reload.addEventListener('click', function () { window.location.reload(); });
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn ghost sm';
    close.textContent = body.dataset.close || 'Close';
    close.addEventListener('click', function () { box.remove(); });
    row.appendChild(reload);
    row.appendChild(close);
    box.appendChild(p);
    box.appendChild(row);
  }
  document.addEventListener('htmx:responseError', function (ev) {
    var status = ev.detail && ev.detail.xhr ? ev.detail.xhr.status : 0;
    toast(status === 403 ? body.dataset.errStale : (body.dataset.errOther || 'HTTP %s').replace('%s', status));
  });
  document.addEventListener('htmx:sendError', function () { toast(body.dataset.errOffline || 'Offline'); });
  var here = document.querySelector('[data-focus]');
  if (here) {
    here.scrollIntoView({ block: 'center' });
    here.focus({ preventScroll: true });
  }
})();
