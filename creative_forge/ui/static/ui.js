/* studio-ui 1.0.0 — shared behaviour for Studio Pulse and Creative Forge (byte-identical in both).
   Theme toggle, tooltips, and a Cmd/Ctrl+K command palette. The palette reads its entries
   from <script type="application/json" id="ui-commands">[{"label","href","hint"}]</script>;
   labels for the theme button come from data-light / data-dark on the button. */
(function () {
  var root = document.documentElement;
  try { var saved = localStorage.getItem('ui-theme'); if (saved) root.setAttribute('data-theme', saved); } catch (e) {}

  function isDark() {
    var t = root.getAttribute('data-theme');
    return t === 'dark' || (!t && window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
  }
  document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
    function label() { btn.textContent = isDark() ? (btn.dataset.light || 'Light') : (btn.dataset.dark || 'Dark'); }
    label();
    btn.addEventListener('click', function () {
      var next = isDark() ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('ui-theme', next); } catch (e) {}
      label();
    });
  });

  var tip = document.getElementById('tip');
  if (!tip) { tip = document.createElement('div'); tip.id = 'tip'; tip.setAttribute('role', 'tooltip'); document.body.appendChild(tip); }
  function show(el, x, y) {
    tip.textContent = el.getAttribute('data-tip');
    tip.style.display = 'block';
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(Math.max(8, x + 12), window.innerWidth - w - 8) + 'px';
    var top = y - h - 12; tip.style.top = (top < 8 ? y + 16 : top) + 'px';
  }
  document.addEventListener('pointermove', function (ev) {
    var el = ev.target.closest && ev.target.closest('[data-tip]');
    if (el) show(el, ev.clientX, ev.clientY); else tip.style.display = 'none';
  });
  document.addEventListener('focusin', function (ev) {
    var el = ev.target.closest && ev.target.closest('[data-tip]');
    if (el) { var r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); }
  });
  document.addEventListener('focusout', function () { tip.style.display = 'none'; });

  var data = document.getElementById('ui-commands');
  if (!data) return;
  var cmds = [];
  try { cmds = JSON.parse(data.textContent); } catch (e) { return; }
  var pal = document.createElement('div');
  pal.className = 'cmdk';
  pal.setAttribute('role', 'dialog');
  pal.setAttribute('aria-modal', 'true');
  pal.innerHTML = '<div class="box"><input type="text" autocomplete="off" spellcheck="false"><ul role="listbox"></ul></div>';
  document.body.appendChild(pal);
  var input = pal.querySelector('input'), list = pal.querySelector('ul'), sel = 0, shown = [];
  input.placeholder = data.dataset.placeholder || 'Search';
  input.setAttribute('aria-label', input.placeholder);
  function fold(s) { return (s || '').toLocaleLowerCase('tr').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/ı/g, 'i'); }
  function render() {
    var q = fold(input.value.trim());
    shown = cmds.filter(function (c) { return !q || fold(c.label + ' ' + (c.hint || '')).indexOf(q) !== -1; }).slice(0, 30);
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    list.innerHTML = '';
    shown.forEach(function (c, i) {
      var li = document.createElement('li'), a = document.createElement('a');
      a.href = c.href; a.textContent = c.label;
      if (c.hint) { var s = document.createElement('small'); s.textContent = c.hint; a.appendChild(s); }
      if (i === sel) a.className = 'sel';
      li.appendChild(a); list.appendChild(li);
    });
  }
  function open() { pal.classList.add('open'); input.value = ''; sel = 0; render(); input.focus(); }
  function close() { pal.classList.remove('open'); }
  document.querySelectorAll('[data-cmdk]').forEach(function (b) { b.addEventListener('click', open); });
  document.addEventListener('keydown', function (ev) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') { ev.preventDefault(); pal.classList.contains('open') ? close() : open(); return; }
    if (!pal.classList.contains('open')) return;
    if (ev.key === 'Escape') { close(); }
    else if (ev.key === 'ArrowDown') { ev.preventDefault(); sel = Math.min(sel + 1, shown.length - 1); render(); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); sel = Math.max(sel - 1, 0); render(); }
    else if (ev.key === 'Enter' && shown[sel]) { ev.preventDefault(); window.location.href = shown[sel].href; }
  });
  input.addEventListener('input', function () { sel = 0; render(); });
  pal.addEventListener('click', function (ev) { if (ev.target === pal) close(); });
})();
