/* Settings: the Python API is the source of truth.
 *
 * Every panel reads and writes one server section through
 * /api/settings/{section}. localStorage only caches the last values so the
 * first paint is right before the server answers. There is no demo value
 * anywhere in this file: an empty profile renders as empty.
 *
 * Inputs bind with data-setting="section.key". Presentation preferences
 * (the customisation section) are owned by theme.js, which calls
 * __imurun_settings.set()/save() to sync them; this module only loads and
 * stores that section.
 *
 * Events on document:
 *   imurun:settings           after every change (detail = all settings)
 *   imurun:settings-hydrated  once, after the server copy has been merged
 */
(function () {
  'use strict';

  var CACHE_KEY = 'imurun.shell.settings.v1';
  var SECTIONS = ['profile', 'permissions', 'server', 'customisation', 'resources', 'voice'];
  /* Loaded (for the first paint) but never saved as a whole section. The
   * `search` section is not cached at all: it holds API keys. */
  var NOT_PUT = { permissions: true };
  var LABELS = {
    voice: 'Voice',
    profile: 'Profile',
    permissions: 'Permissions',
    server: 'Server settings',
    customisation: 'Customisation',
    resources: 'Resources'
  };

  /* Mirrors services/server/settings.py. Only used before the first fetch. */
  var defaults = {
    profile: { name: '', email: '', role: 'Local', timezone: '' },
    permissions: {
      readFiles: true,
      writeFiles: true,
      runShell: true,
      network: true,
      sendEmail: true
    },
    server: {
      port: 8732,
      logLevel: 'Info',
      contextSize: 8192,
      gpuLayers: -1,
      threads: 0,
      loadMode: 'mmap',
      lazyMode: 'auto',
      mmap: true,
      flashAttention: true,
      kvCacheType: 'f16',
      maxTokens: 2048,
      temperature: 0.7,
      topP: 0.9,
      repeatPenalty: 1.1,
      seed: -1,
      thinkingLevel: 'balanced',
      startWithShell: true
    },
    customisation: {
      theme: 'imutechz',
      font: 'default',
      fontScale: 1,
      backgroundMotion: 'full',
      ambientMode: 'mixed',
      glassIntensity: 'frosted',
      density: 'comfortable',
      accentColor: '',
      accentCustom: false,
      notificationDuration: 4500,
      reducedMotion: false
    },
    resources: { cacheLimitGb: 40, transcriptDays: 30, logDays: 7 },
    /* Not in the server's DEFAULTS yet; SettingsStore accepts any valid
     * section name and returns {} for a new one, merged with these. */
    voice: { lang: '', voiceURI: '', rate: 1, sounds: true }
  };

  function merge(a, b) {
    var out = {};
    var key;
    for (key in a) {
      if (a[key] && typeof a[key] === 'object' && !Array.isArray(a[key])) {
        out[key] = merge(a[key], (b && b[key]) || {});
      } else {
        out[key] = b && b[key] !== undefined ? b[key] : a[key];
      }
    }
    if (b) {
      for (key in b) {
        if (!(key in out)) out[key] = b[key];
      }
    }
    return out;
  }

  function readCache() {
    try {
      var raw = localStorage.getItem(CACHE_KEY);
      if (raw) return merge(defaults, JSON.parse(raw));
    } catch (error) { /* a corrupt cache is not fatal */ }
    return merge(defaults, {});
  }

  function writeCache(values) {
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(values));
    } catch (error) { /* private mode: the server still has the truth */ }
  }

  function getNested(obj, path) {
    return path.split('.').reduce(function (value, key) {
      return value && value[key] !== undefined ? value[key] : undefined;
    }, obj);
  }

  function setNested(obj, path, value) {
    var parts = path.split('.');
    var last = parts.pop();
    var target = parts.reduce(function (node, key) {
      if (!node[key] || typeof node[key] !== 'object') node[key] = {};
      return node[key];
    }, obj);
    target[last] = value;
  }

  function sectionOf(path) {
    return String(path).split('.')[0];
  }

  function coerce(raw, current, input) {
    if (typeof current === 'boolean') {
      return raw === true || raw === 'true' || raw === 'on' || raw === '1';
    }
    if (typeof current === 'number' || (input && input.type === 'number')) {
      var number = Number(raw);
      return Number.isFinite(number) ? number : current;
    }
    return raw;
  }

  function toast(options) {
    if (typeof window.showNotification === 'function') window.showNotification(options);
  }

  var settings = readCache();

  /* ------------------------------------------------------------- rendering */

  function apply() {
    var profile = settings.profile || {};
    var name = (profile.name || '').trim();
    var email = (profile.email || '').trim();
    var role = (profile.role || 'Local').trim() || 'Local';

    var avatar = document.querySelector('.profile-avatar');
    if (avatar) avatar.textContent = name ? name.slice(0, 2).toUpperCase() : 'IM';
    var nameEl = document.querySelector('.profile-row strong');
    if (nameEl) nameEl.textContent = name || 'Local workspace';
    var muted = document.querySelector('.profile-row .muted');
    if (muted) muted.textContent = email || 'Not signed in';
    var roleChip = document.querySelector('.profile-row .chip');
    if (roleChip) {
      roleChip.textContent = role;
      roleChip.className = 'chip ' + (role === 'Owner' ? 'success' : 'neutral');
    }

    var endpoint = document.getElementById('api-endpoint');
    if (endpoint && window.__imurun_api) {
      window.__imurun_api.baseUrl().then(function (base) {
        endpoint.textContent = base || window.location.origin;
      }, function () {
        endpoint.textContent = window.location.origin;
      });
    }

    try {
      document.dispatchEvent(new CustomEvent('imurun:settings', { detail: settings }));
    } catch (error) { /* older browsers */ }
  }

  function populate() {
    document.querySelectorAll('[data-setting]').forEach(function (input) {
      /* Never overwrite a field the user is editing right now. */
      if (input === document.activeElement) return;
      var path = input.dataset.setting;
      var value = getNested(settings, path);
      if (value === undefined) return;
      if (input.type === 'checkbox') {
        input.checked = Boolean(value);
      } else if (input.tagName === 'SELECT' && typeof value === 'boolean') {
        input.value = value ? 'true' : 'false';
      } else {
        input.value = value;
      }
    });
  }

  /* ----------------------------------------------------------------- saves */

  var pendingTimers = {};
  var inflight = {};

  /* Save one section. Resolves true on success, false on failure (and shows
   * the error once). A debounced save can be flushed on unload. */
  function persist(section, immediate, options) {
    if (!section || !window.__imurun_api) return Promise.resolve(false);
    /* `permissions` is written through POST /api/permissions (validated) by
     * agent-settings.js. A whole-section PUT of this page's cached copy
     * would write stale toggles back over a newer change. */
    if (NOT_PUT[section]) return Promise.resolve(false);
    var run = function () {
      pendingTimers[section] = null;
      var values = settings[section] || {};
      var request = window.__imurun_api.updateSettings(section, values, options).then(function () {
        return true;
      }, function (error) {
        toast({
          id: 'settings-save-' + section, timeout: 7000,
          tone: 'error', icon: 'alert', kicker: 'Settings',
          title: (LABELS[section] || section) + ' not saved',
          text: error.message + '. Your change is kept on this page; save again once the server answers.'
        });
        return false;
      });
      inflight[section] = request;
      return request;
    };
    if (pendingTimers[section]) {
      clearTimeout(pendingTimers[section]);
      pendingTimers[section] = null;
    }
    if (immediate) return run();
    pendingTimers[section] = setTimeout(run, 350);
    return Promise.resolve(true);
  }

  function flushPending() {
    Object.keys(pendingTimers).forEach(function (section) {
      if (pendingTimers[section]) persist(section, true, { keepalive: true });
    });
  }

  function commit(path, raw, input) {
    var current = getNested(settings, path);
    var value = coerce(raw, current, input);
    setNested(settings, path, value);
    writeCache(settings);
    apply();
    persist(sectionOf(path));
    return value;
  }

  function bindInputs() {
    document.querySelectorAll('[data-setting]').forEach(function (input) {
      if (input.dataset.bound === '1') return;
      input.dataset.bound = '1';
      var path = input.dataset.setting;
      var event = input.type === 'color' ? 'input' : 'change';
      input.addEventListener(event, function () {
        if (input.type === 'checkbox') commit(path, input.checked, input);
        else commit(path, input.value, input);
      });
    });
  }

  /* Save buttons name their section by the panel they sit in. Before, the
   * section was derived from the button label ("Server settings"), which
   * matched no section, so every Save button silently did nothing. */
  function sectionForButton(button) {
    if (button.dataset.settingsSection && SECTIONS.indexOf(button.dataset.settingsSection) !== -1) {
      return button.dataset.settingsSection;
    }
    var panel = button.closest('[data-settings-panel]');
    if (panel && SECTIONS.indexOf(panel.dataset.settingsPanel) !== -1) return panel.dataset.settingsPanel;
    var guess = String(button.dataset.saveSettings || '').toLowerCase().split(' ')[0];
    return SECTIONS.indexOf(guess) !== -1 ? guess : null;
  }

  function bindSaveButtons() {
    document.querySelectorAll('[data-save-settings]').forEach(function (button) {
      if (button.dataset.bound === '1') return;
      button.dataset.bound = '1';
      button.addEventListener('click', function () {
        var section = sectionForButton(button);
        if (!section) return;
        var label = button.textContent;
        button.disabled = true;
        button.textContent = 'Saving…';
        persist(section, true).then(function (ok) {
          button.disabled = false;
          button.textContent = label;
          if (!ok) return;
          toast({
            tone: 'success', icon: 'check', kicker: 'Saved',
            title: (button.dataset.saveSettings || LABELS[section]) + ' saved',
            text: 'Synced to the local API.'
          });
        });
      });
    });
  }

  /* ---------------------------------------------------------------- hydrate */

  var ready = (async function hydrate() {
    apply();
    populate();
    bindInputs();
    bindSaveButtons();

    if (!window.__imurun_api) return settings;

    var results = await Promise.allSettled(
      SECTIONS.map(function (section) {
        return window.__imurun_api.getSettings(section);
      })
    );

    results.forEach(function (result, index) {
      if (result.status !== 'fulfilled') return;
      var payload = result.value;
      var values = payload && payload.values ? payload.values : payload;
      if (!values || typeof values !== 'object') return;
      settings[SECTIONS[index]] = merge(defaults[SECTIONS[index]], values);
    });

    writeCache(settings);
    apply();
    populate();
    bindInputs();
    try {
      document.dispatchEvent(new CustomEvent('imurun:settings-hydrated', { detail: settings }));
    } catch (error) { /* ignore */ }
    return settings;
  })();

  window.addEventListener('pagehide', flushPending);

  window.__imurun_settings = {
    get: function () { return settings; },
    /* options.silent: do not re-populate inputs (used by theme.js) */
    set: function (partial, options) {
      settings = merge(settings, partial || {});
      writeCache(settings);
      apply();
      if (!options || !options.silent) populate();
    },
    save: function (section) {
      if (section) return persist(section, true);
      return Promise.all(SECTIONS.map(function (name) { return persist(name, true); }));
    },
    defaults: defaults,
    ready: ready
  };
})();
