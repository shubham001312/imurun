/* Theme system: the one owner of presentation preferences.
 *
 * A theme is a stylesheet in styles/ that sets the shared custom properties
 * (--bg, --surface, --surface-2, --text, --muted, --line, --accent, ...).
 * Everything else is a preference layered on top through <body> data
 * attributes and inline custom properties, styled by styles/customise.css:
 *
 *   theme             <link id="theme-link"> href, body[data-theme]
 *   font              body[data-font]            (fonts.css)
 *   fontScale         --font-scale               (chat text size)
 *   density           body[data-density]         compact | comfortable | spacious
 *   accentColor       --accent and derived tokens, only when accentCustom
 *   reducedMotion     body[data-reduced-motion]  forces reduced motion
 *   backgroundMotion  body[data-motion]          full | washes | drift | none
 *   ambientMode       body[data-ambient]         mixed | warm | cool | none
 *   glassIntensity    body[data-glass]           frosted | clear | tinted
 *   notificationDuration  ms toasts stay up
 *
 * Persistence: localStorage (instant, first paint) and the server
 * `customisation` settings section (shared across browsers). A change made
 * before the server answers wins over the server copy.
 *
 * Public: IMURUN.theme.{get, set, themes, apply}
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var store = IM.store;
  var KEY = 'imurun.prefs.customisation.v1';
  var LEGACY_THEME_KEY = 'imurun.design.theme.v3';
  var LEGACY_FONT_KEY = 'imurun.design.font.v2';

  /* swatch: [background, surface, accent] for the picker preview. */
  var THEMES = [
    { id: 'imutechz', label: 'ImuTechz', note: 'The company look: cream paper, ink, brand red', swatch: ['#f3ede2', '#faf7f0', '#d9481e'], dark: false },
    { id: 'imutechz-dark', label: 'ImuTechz Console', note: 'The company look, dark', swatch: ['#09090b', '#111113', '#ff5c35'], dark: true },
    { id: 'liquid-paper', label: 'Liquid Paper', note: 'Light glass, warm, serif headings', swatch: ['#f3ede2', '#fffaf2', '#b45309'], dark: false },
    { id: 'paper', label: 'Paper', note: 'Light, warm, editorial', swatch: ['#f6f2ea', '#fffdf8', '#b45309'], dark: false },
    { id: 'slate', label: 'Slate', note: 'Light, cool, clean', swatch: ['#f1f5f9', '#ffffff', '#2563eb'], dark: false },
    { id: 'liquid-slate', label: 'Liquid Slate', note: 'Light glass, cool', swatch: ['#eef3f9', '#ffffff', '#2563eb'], dark: false },
    { id: 'liquid', label: 'Liquid', note: 'Dark glass with pointer light', swatch: ['#070b18', '#1a2033', '#8be9ff'], dark: true },
    { id: 'obsidian', label: 'Obsidian', note: 'Dark, amber, dense', swatch: ['#0b0b0d', '#17171c', '#f5a524'], dark: true },
    { id: 'aurora', label: 'Aurora', note: 'Dark, soft, rounded', swatch: ['#0a0f1e', '#1e2946', '#7dd3fc'], dark: true },
    { id: 'neon', label: 'Neon', note: 'Dark, cyan and violet glow', swatch: ['#070a14', '#18213a', '#22d3ee'], dark: true },
    { id: 'phosphor', label: 'Phosphor', note: 'CRT green, monospace', swatch: ['#040704', '#0b130b', '#39ff14'], dark: true }
  ];

  var FONTS = [
    { id: 'manrope', label: 'Manrope' },
    { id: 'inter', label: 'Inter' },
    { id: 'grotesk', label: 'Space Grotesk' },
    { id: 'default', label: 'Theme default' }
  ];

  var ACCENT_PRESETS = ['#b45309', '#be123c', '#7c3aed', '#2563eb', '#0f766e', '#15803d'];

  /* `chosen` marks a theme or font the user picked. Without it, the old
   * defaults (Liquid Paper, Manrope: what every earlier install stored and
   * what the server's `customisation` defaults still say) are read as "no
   * choice" and become the ImuTechz defaults (BUGS-FIXED.md 66). */
  var LEGACY_DEFAULTS = { theme: 'liquid-paper', font: 'manrope' };
  var DEFAULTS = {
    theme: 'imutechz',
    font: 'default',
    chosen: false,
    fontScale: 1,
    density: 'comfortable',
    accentColor: '',
    accentCustom: false,
    reducedMotion: false,
    backgroundMotion: 'full',
    ambientMode: 'mixed',
    glassIntensity: 'frosted',
    notificationDuration: 4500
  };

  var DENSITIES = ['compact', 'comfortable', 'spacious'];

  function clone(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  function themeById(id) {
    return THEMES.filter(function (theme) { return theme.id === id; })[0] || null;
  }

  /* Coerce whatever arrived (cache, server, URL) into valid values. */
  function normalise(raw) {
    var src = raw || {};
    var out = clone(DEFAULTS);
    var chosen = src.chosen === true || src.chosen === 'true';
    out.chosen = chosen;
    if (themeById(src.theme) && (chosen || src.theme !== LEGACY_DEFAULTS.theme)) out.theme = src.theme;
    if (FONTS.some(function (f) { return f.id === src.font; }) && (chosen || src.font !== LEGACY_DEFAULTS.font)) out.font = src.font;
    var scale = Number(src.fontScale);
    if (isFinite(scale) && scale >= 0.85 && scale <= 1.3) out.fontScale = Math.round(scale * 100) / 100;
    if (DENSITIES.indexOf(src.density) !== -1) out.density = src.density;
    if (/^#[0-9a-f]{6}$/i.test(src.accentColor || '')) out.accentColor = src.accentColor.toLowerCase();
    /* The server's default accentColor is Liquid Paper's amber, so a stored
     * colour only overrides the theme when the user picked it on purpose. */
    out.accentCustom = Boolean(src.accentCustom) && Boolean(out.accentColor);
    out.reducedMotion = src.reducedMotion === true || src.reducedMotion === 'true';
    if (['full', 'washes', 'drift', 'none'].indexOf(src.backgroundMotion) !== -1) out.backgroundMotion = src.backgroundMotion;
    if (['mixed', 'warm', 'cool', 'none'].indexOf(src.ambientMode) !== -1) out.ambientMode = src.ambientMode;
    if (['frosted', 'clear', 'tinted'].indexOf(src.glassIntensity) !== -1) out.glassIntensity = src.glassIntensity;
    var duration = Number(src.notificationDuration);
    if (isFinite(duration) && duration >= 1000 && duration <= 30000) out.notificationDuration = duration;
    return out;
  }

  function initialPrefs() {
    var saved = store.get(KEY, null);
    if (!saved) {
      /* Migrate the older keys (app.js theme switcher, settings cache). */
      var legacy = store.get('imurun.shell.settings.v1', null);
      saved = (legacy && legacy.customisation) ? Object.assign({}, legacy.customisation) : {};
      saved.accentCustom = false;
      try {
        var oldTheme = window.localStorage.getItem(LEGACY_THEME_KEY);
        var oldFont = window.localStorage.getItem(LEGACY_FONT_KEY);
        if (oldTheme && !saved.theme) saved.theme = oldTheme;
        if (oldFont && !saved.font) saved.font = oldFont;
      } catch (error) { /* ignore */ }
    }
    return normalise(saved);
  }

  var prefs = initialPrefs();
  var dirty = false;          /* changed locally since page load */
  var hydrated = false;       /* server copy has been seen */
  var urlOverride = {};       /* ?style= / ?font= preview, never persisted */

  try {
    var params = new URLSearchParams(location.search);
    if (themeById(params.get('style'))) urlOverride.theme = params.get('style');
    if (FONTS.some(function (f) { return f.id === params.get('font'); })) urlOverride.font = params.get('font');
  } catch (error) { /* ignore */ }

  /* ------------------------------------------------------------- applying */

  function relativeLuminance(hex) {
    var n = parseInt(hex.slice(1), 16);
    var channels = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(function (c) {
      var v = c / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  }

  function swapStylesheet(id) {
    var link = document.getElementById('theme-link');
    var href = 'styles/' + id + '.css';
    if (!link || link.getAttribute('href') === href) return;
    /* Load the new sheet next to the old one and drop the old one only when
     * the new one is ready, so a switch never flashes unstyled variables. */
    var next = document.createElement('link');
    next.rel = 'stylesheet';
    next.href = href;
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      if (link.parentNode) link.parentNode.removeChild(link);
      next.id = 'theme-link';
    }
    next.addEventListener('load', finish);
    next.addEventListener('error', finish);
    link.parentNode.insertBefore(next, link.nextSibling);
    setTimeout(finish, 1500);
  }

  function apply(options) {
    var effective = Object.assign({}, prefs, urlOverride);
    var body = document.body;
    if (!body) return;
    var animate = options && options.animate && !IM.util.reducedMotion();
    if (animate) {
      document.documentElement.classList.add('theme-switching');
      setTimeout(function () { document.documentElement.classList.remove('theme-switching'); }, 420);
    }

    swapStylesheet(effective.theme);
    body.dataset.theme = effective.theme;
    var theme = themeById(effective.theme);
    body.dataset.scheme = theme && theme.dark ? 'dark' : 'light';

    if (effective.font && effective.font !== 'default') body.dataset.font = effective.font;
    else body.removeAttribute('data-font');

    body.dataset.density = effective.density;
    body.dataset.motion = effective.backgroundMotion;
    body.dataset.ambient = effective.ambientMode;
    body.dataset.glass = effective.glassIntensity;
    body.dataset.reducedMotion = effective.reducedMotion ? '1' : '0';
    body.style.setProperty('--font-scale', String(effective.fontScale));

    /* An old build pinned --accent on <html>; clear it so themes win. */
    document.documentElement.style.removeProperty('--accent');
    if (effective.accentCustom && effective.accentColor) {
      var accent = effective.accentColor;
      body.dataset.accent = 'custom';
      body.style.setProperty('--accent', accent);
      body.style.setProperty('--accent-soft', 'color-mix(in srgb, ' + accent + ' 12%, transparent)');
      body.style.setProperty('--accent-line', 'color-mix(in srgb, ' + accent + ' 36%, transparent)');
      body.style.setProperty('--focus-ring', '0 0 0 3px color-mix(in srgb, ' + accent + ' 20%, transparent)');
      body.style.setProperty('--accent-text', relativeLuminance(accent) > 0.45 ? '#16110a' : '#ffffff');
    } else {
      body.removeAttribute('data-accent');
      ['--accent', '--accent-soft', '--accent-line', '--focus-ring', '--accent-text'].forEach(function (name) {
        body.style.removeProperty(name);
      });
    }

    window.__imurun_notify_timeout = effective.notificationDuration;
    syncControls();
    if (IM.bus) IM.bus.emit('theme', effective);
  }

  /* ---------------------------------------------------------- persistence */

  var pushToServer = IM.util.debounce(function () {
    var settings = window.__imurun_settings;
    if (!settings) return;
    settings.set({ customisation: clone(prefs) }, { silent: true });
    settings.save('customisation');
  }, 400);

  function set(partial, options) {
    var patch = Object.assign({}, partial || {});
    if ((patch.theme || patch.font) && patch.chosen === undefined) patch.chosen = true;
    var next = normalise(Object.assign({}, prefs, patch));
    /* Picking a theme drops a temporary ?style= preview. */
    if (partial && partial.theme) delete urlOverride.theme;
    if (partial && partial.font) delete urlOverride.font;
    prefs = next;
    store.set(KEY, prefs);
    dirty = true;
    apply({ animate: Boolean(partial && partial.theme) });
    if (!options || options.persist !== false) pushToServer();
    return prefs;
  }

  /* Settings hydrated from the server: adopt its copy unless the user
   * already changed something on this page, in which case ours wins. */
  document.addEventListener('imurun:settings-hydrated', function (event) {
    hydrated = true;
    var server = event.detail && event.detail.customisation;
    if (dirty) {
      pushToServer();
      return;
    }
    if (!server) return;
    var savedLocally = store.get(KEY, null);
    var incoming = Object.assign({}, server);
    /* A server copy written by an older shell has no accentCustom flag; keep
     * the local decision about the accent in that case. */
    if (incoming.accentCustom === undefined) {
      incoming.accentCustom = Boolean(savedLocally && savedLocally.accentCustom);
      if (!incoming.accentCustom) incoming.accentColor = savedLocally ? savedLocally.accentColor : '';
    }
    var next = normalise(Object.assign({}, prefs, incoming));
    if (JSON.stringify(next) === JSON.stringify(prefs)) return;
    prefs = next;
    store.set(KEY, prefs);
    apply();
  });

  /* ------------------------------------------------------------ controls */

  function buildThemePicker() {
    var host = document.getElementById('theme-switcher');
    if (!host || host.dataset.built === '1') return;
    host.dataset.built = '1';
    host.setAttribute('role', 'radiogroup');
    THEMES.forEach(function (theme) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'theme-card';
      button.dataset.themeId = theme.id;
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-label', theme.label + ', ' + theme.note);
      button.innerHTML =
        '<span class="theme-swatch" aria-hidden="true">' +
          '<i data-c="0"></i><i data-c="1"></i><i data-c="2"></i>' +
        '</span>' +
        '<span class="theme-card-text"><strong></strong><small></small></span>';
      var swatches = button.querySelectorAll('.theme-swatch i');
      for (var i = 0; i < swatches.length; i += 1) swatches[i].style.background = theme.swatch[i];
      button.querySelector('strong').textContent = theme.label;
      button.querySelector('small').textContent = theme.note;
      button.addEventListener('click', function () { set({ theme: theme.id }); });
      host.appendChild(button);
    });
    host.addEventListener('keydown', function (event) {
      if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].indexOf(event.key) === -1) return;
      var cards = Array.prototype.slice.call(host.querySelectorAll('.theme-card'));
      var index = cards.indexOf(document.activeElement);
      if (index === -1) return;
      event.preventDefault();
      var step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1;
      var next = cards[(index + step + cards.length) % cards.length];
      next.focus();
      next.click();
    });
  }

  function buildFontPicker() {
    var host = document.getElementById('font-switcher');
    if (!host || host.dataset.built === '1') return;
    host.dataset.built = '1';
    host.setAttribute('role', 'radiogroup');
    FONTS.forEach(function (font) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'theme-btn';
      button.dataset.fontId = font.id;
      button.setAttribute('role', 'radio');
      button.textContent = font.label;
      button.addEventListener('click', function () { set({ font: font.id }); });
      host.appendChild(button);
    });
  }

  function buildAccentPresets() {
    var host = document.getElementById('accent-presets');
    if (!host || host.dataset.built === '1') return;
    host.dataset.built = '1';
    ACCENT_PRESETS.forEach(function (color) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'accent-chip';
      button.dataset.accent = color;
      button.style.background = color;
      button.setAttribute('aria-label', 'Accent ' + color);
      button.addEventListener('click', function () { set({ accentColor: color, accentCustom: true }); });
      host.appendChild(button);
    });
  }

  function bindPrefControls() {
    document.querySelectorAll('[data-pref]').forEach(function (input) {
      if (input.dataset.prefBound === '1') return;
      input.dataset.prefBound = '1';
      var key = input.dataset.pref;
      var eventName = (input.type === 'range' || input.type === 'color') ? 'input' : 'change';
      input.addEventListener(eventName, function () {
        var patch = {};
        if (input.type === 'checkbox') patch[key] = input.checked;
        else if (input.type === 'range' || input.type === 'number') patch[key] = Number(input.value);
        else if (input.type === 'radio') {
          if (!input.checked) return;
          patch[key] = input.value;
        } else patch[key] = input.value;
        if (key === 'accentColor') patch.accentCustom = true;
        set(patch);
      });
    });
    var reset = document.getElementById('accent-reset');
    if (reset && reset.dataset.prefBound !== '1') {
      reset.dataset.prefBound = '1';
      reset.addEventListener('click', function () { set({ accentCustom: false }); });
    }
    var resetAll = document.getElementById('prefs-reset');
    if (resetAll && resetAll.dataset.prefBound !== '1') {
      resetAll.dataset.prefBound = '1';
      resetAll.addEventListener('click', function () {
        set(clone(DEFAULTS));
        if (IM.toast) IM.toast({ tone: 'neutral', icon: 'info', kicker: 'Customisation', title: 'Look and feel reset', text: 'Theme, text, density and motion are back to their defaults.' });
      });
    }
  }

  /* Reflect the effective preferences in every control. */
  function syncControls() {
    var effective = Object.assign({}, prefs, urlOverride);
    document.querySelectorAll('.theme-card').forEach(function (card) {
      var active = card.dataset.themeId === effective.theme;
      card.classList.toggle('active', active);
      card.setAttribute('aria-checked', active ? 'true' : 'false');
      card.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('[data-font-id]').forEach(function (button) {
      var active = button.dataset.fontId === effective.font;
      button.classList.toggle('active', active);
      button.setAttribute('aria-checked', active ? 'true' : 'false');
    });
    document.querySelectorAll('.accent-chip').forEach(function (chip) {
      chip.classList.toggle('active', effective.accentCustom && chip.dataset.accent === effective.accentColor);
    });
    document.querySelectorAll('[data-pref]').forEach(function (input) {
      if (input === document.activeElement && input.type !== 'radio' && input.type !== 'checkbox') return;
      var key = input.dataset.pref;
      var value = effective[key];
      if (key === 'accentColor') {
        value = effective.accentCustom ? effective.accentColor : currentThemeAccent();
      }
      if (input.type === 'checkbox') input.checked = Boolean(value);
      else if (input.type === 'radio') input.checked = String(value) === input.value;
      else input.value = value;
    });
    var scaleOut = document.getElementById('pref-font-scale-out');
    if (scaleOut) scaleOut.textContent = Math.round(effective.fontScale * 100) + '%';
    var note = document.getElementById('lab-note');
    var theme = themeById(effective.theme);
    if (note && theme) {
      note.textContent = theme.label + ': ' + theme.note + (urlOverride.theme ? ' (preview from the link, not saved)' : '') + '.';
    }
    var reset = document.getElementById('accent-reset');
    if (reset) reset.disabled = !effective.accentCustom;
  }

  /* The theme's own accent as a #rrggbb string, for the colour input. */
  function currentThemeAccent() {
    var theme = themeById(Object.assign({}, prefs, urlOverride).theme);
    return theme ? theme.swatch[2] : '#b45309';
  }

  function init() {
    buildThemePicker();
    buildFontPicker();
    buildAccentPresets();
    bindPrefControls();
    apply();
    /* OS reduced-motion changes must reach JS-driven motion as well. */
    try {
      var media = window.matchMedia('(prefers-reduced-motion: reduce)');
      var onChange = function () { if (IM.bus) IM.bus.emit('theme', Object.assign({}, prefs, urlOverride)); };
      if (media.addEventListener) media.addEventListener('change', onChange);
      else if (media.addListener) media.addListener(onChange);
    } catch (error) { /* ignore */ }
  }

  IM.theme = {
    themes: THEMES,
    fonts: FONTS,
    defaults: clone(DEFAULTS),
    get: function () { return Object.assign({}, prefs, urlOverride); },
    set: set,
    apply: apply,
    isHydrated: function () { return hydrated; }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
