/* Theme switcher and popup demo for the design lab. */
(function () {
  var THEMES = [
    { id: 'obsidian', file: 'styles/obsidian.css', label: 'Obsidian', note: 'dark · amber · dense' },
    { id: 'paper',    file: 'styles/paper.css',    label: 'Paper',    note: 'light · warm · editorial' },
    { id: 'neon',     file: 'styles/neon.css',     label: 'Neon',     note: 'dark · cyan/violet · glass' },
    { id: 'slate',    file: 'styles/slate.css',    label: 'Slate',    note: 'light · cool · clean' },
    { id: 'phosphor', file: 'styles/phosphor.css', label: 'Phosphor', note: 'CRT · green · mono' },
    { id: 'aurora',   file: 'styles/aurora.css',   label: 'Aurora',   note: 'dark · soft · rounded' }
  ];

  var link = document.getElementById('theme-link');
  var note = document.getElementById('lab-note');
  var switcher = document.getElementById('theme-switcher');
  var buttons = {};

  function select(theme) {
    link.href = theme.file;
    document.body.dataset.theme = theme.id;
    note.textContent = theme.label + ' — ' + theme.note;
    Object.keys(buttons).forEach(function (id) {
      buttons[id].classList.toggle('active', id === theme.id);
    });
    try { localStorage.setItem('imurun.design.theme', theme.id); } catch (e) { /* file:// may refuse */ }
  }

  THEMES.forEach(function (theme) {
    var b = document.createElement('button');
    b.className = 'theme-btn';
    b.type = 'button';
    b.textContent = theme.label;
    b.addEventListener('click', function () { select(theme); });
    switcher.appendChild(b);
    buttons[theme.id] = b;
  });

  var saved = null;
  try { saved = localStorage.getItem('imurun.design.theme'); } catch (e) { /* ignore */ }
  var fromUrl = null;
  try { fromUrl = new URLSearchParams(location.search).get('style'); } catch (e) { /* ignore */ }
  var wanted = fromUrl || saved;
  var start = THEMES.filter(function (t) { return t.id === wanted; })[0] || THEMES[0];
  select(start);

  /* Popup demo */
  var modal = document.getElementById('approval');
  var toast = document.getElementById('toast');
  var toastTimer = null;

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 2200);
  }

  function openModal() {
    modal.hidden = false;
    var first = document.getElementById('approval-cancel');
    if (first) first.focus();
  }

  function closeModal(result) {
    modal.hidden = true;
    if (result) showToast(result);
  }

  document.getElementById('open-approval').addEventListener('click', openModal);
  document.getElementById('approval-cancel').addEventListener('click', function () {
    closeModal('cancelled — recorded as refused');
  });
  document.getElementById('approval-send').addEventListener('click', function () {
    closeModal('sent — action approved once');
  });
  modal.addEventListener('click', function (event) {
    if (event.target === modal) closeModal('cancelled — recorded as refused');
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !modal.hidden) closeModal('cancelled — recorded as refused');
  });

  var popup = null;
  try { popup = new URLSearchParams(location.search).get('popup'); } catch (e) { /* ignore */ }
  if (popup === '1') openModal();
})();
