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
