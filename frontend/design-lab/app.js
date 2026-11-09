/* Shell: navigation, sessions, home, connectors, skills, persona, settings
 * actions, resources, clients, usage, and routing of server events.
 *
 * Chat lives in chat.js, the theme in theme.js, toasts in toast.js, models
 * in models.js, the stats/search/shortcut panels in panels.js.
 *
 *   IMURUN.shell.setPage(page)
 *   IMURUN.shell.openSettings(section)
 *   IMURUN.shell.createSession(firstCommand) -> Promise<session|null>
 *   IMURUN.shell.activateSession(id)
 *   IMURUN.shell.handleCommand(text) -> bool   (/rename etc.)
 *   IMURUN.shell.refreshHome()
 *
 * Lists are re-rendered with innerHTML and handled by ONE delegated listener
 * per container, bound once at start-up, so re-renders never stack
 * listeners.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var state = IM.state;
  var escapeHtml = util.escapeHtml;
  var api = window.__imurun_api;

  function toast(options) { return IM.toast ? IM.toast(options) : null; }
  function byId(id) { return document.getElementById(id); }

  /* ------------------------------------------------------- pointer light */

  (function pointerGlow() {
    var glow = byId('pointer-glow');
    if (!glow) return;
    var frame = null;
    var px = 0;
    var py = 0;
    document.addEventListener('pointermove', function (event) {
      if (util.reducedMotion()) return;
      px = event.clientX;
      py = event.clientY;
      if (frame) return;
      frame = requestAnimationFrame(function () {
        frame = null;
        document.documentElement.style.setProperty('--px', px + 'px');
        document.documentElement.style.setProperty('--py', py + 'px');
      });
    }, { passive: true });
  })();

  /* ------------------------------------------------- sidebar and drawer */

  var shell = document.querySelector('.shell');
  var sidebarToggle = byId('sidebar-toggle');
  var navToggle = byId('nav-toggle');
  var SIDEBAR_KEY = 'imurun.design.sidebar';
  var mobileQuery = window.matchMedia ? window.matchMedia('(max-width: 860px)') : null;

  function isMobile() { return Boolean(mobileQuery && mobileQuery.matches); }

  function setSidebar(collapsed) {
    if (!shell) return;
    shell.classList.toggle('sidebar-collapsed', collapsed);
    if (sidebarToggle) {
      sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      sidebarToggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    }
    IM.store.set(SIDEBAR_KEY, collapsed ? '1' : '0');
  }

  var releaseDrawer = null;
  function setDrawer(open) {
    if (!shell) return;
    if (IM.sound && shell.classList.contains('nav-open') !== Boolean(open)) { if (open) IM.sound.open(); else IM.sound.close(); }
    shell.classList.toggle('nav-open', open);
    if (navToggle) navToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    var sidebar = byId('sidebar');
    if (open && sidebar) {
      releaseDrawer = util.trapFocus(sidebar);
      var first = sidebar.querySelector('.nav-item');
      if (first) first.focus();
    } else if (releaseDrawer) {
      releaseDrawer();
      releaseDrawer = null;
    }
  }

  if (sidebarToggle) {
    sidebarToggle.addEventListener('click', function () {
      if (isMobile()) {
        setDrawer(false);
        return;
      }
      setSidebar(!shell.classList.contains('sidebar-collapsed'));
    });
  }
  if (navToggle) navToggle.addEventListener('click', function () { setDrawer(!shell.classList.contains('nav-open')); });
  var scrim = byId('nav-scrim');
  if (scrim) scrim.addEventListener('click', function () { setDrawer(false); });
  if (IM.store.get(SIDEBAR_KEY, '0') === '1') setSidebar(true);
  if (mobileQuery) {
    var onMedia = function () { if (!isMobile()) setDrawer(false); };
    if (mobileQuery.addEventListener) mobileQuery.addEventListener('change', onMedia);
    else if (mobileQuery.addListener) mobileQuery.addListener(onMedia);
  }

  /* ---------------------------------------------------------------- pages */

  var pageNav = document.querySelectorAll('.nav-item[data-page]');
  var pageViews = document.querySelectorAll('.page-view');
  var pageCrumb = byId('page-crumb');
  var settingsTabs = document.querySelectorAll('[data-settings-section]');
  var settingsPanels = document.querySelectorAll('[data-settings-panel]');
  var currentSettingsSection = 'profile';
  var currentPage = 'home';

  function settingsLabel(section) {
    var tab = document.querySelector('.settings-tab[data-settings-section="' + section + '"]');
    return tab ? tab.textContent.trim() : section;
  }

  function setSettingsSection(section, focusTab) {
    if (!document.querySelector('[data-settings-panel="' + section + '"]')) section = 'profile';
    currentSettingsSection = section;
    settingsTabs.forEach(function (tab) {
      var active = tab.dataset.settingsSection === section;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
      if (active && focusTab) tab.focus();
    });
    settingsPanels.forEach(function (panel) {
      var show = panel.dataset.settingsPanel === section;
      if (show && panel.hidden && !util.reducedMotion()) {
        panel.classList.remove('is-entering');
        void panel.offsetWidth;
        panel.classList.add('is-entering');
      }
      panel.hidden = !show;
    });
    if (pageCrumb && currentPage === 'settings') pageCrumb.textContent = 'Settings / ' + settingsLabel(section);
    IM.bus.emit('settings.section', section);
  }

  var subnav = document.querySelector('.settings-subnav');
  if (subnav) {
    subnav.setAttribute('role', 'tablist');
    subnav.setAttribute('aria-orientation', 'vertical');
    settingsTabs.forEach(function (tab) {
      tab.setAttribute('role', 'tab');
      tab.id = tab.id || 'settings-tab-' + tab.dataset.settingsSection;
      var panel = document.querySelector('[data-settings-panel="' + tab.dataset.settingsSection + '"]');
      if (panel) {
        panel.setAttribute('role', 'tabpanel');
        panel.setAttribute('aria-labelledby', tab.id);
        tab.setAttribute('aria-controls', panel.id = panel.id || 'settings-panel-' + tab.dataset.settingsSection);
      }
    });
    subnav.addEventListener('click', function (event) {
      var tab = event.target.closest('[data-settings-section]');
      if (tab) setSettingsSection(tab.dataset.settingsSection);
    });
    subnav.addEventListener('keydown', function (event) {
      var keys = ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (keys.indexOf(event.key) === -1) return;
      var tabs = Array.prototype.slice.call(subnav.querySelectorAll('[data-settings-section]'));
      var index = tabs.indexOf(document.activeElement);
      if (index === -1) return;
      event.preventDefault();
      var next = index;
      if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else next = (index + (event.key === 'ArrowDown' || event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      setSettingsSection(tabs[next].dataset.settingsSection, true);
    });
  }

  function renderChatCrumb() {
    if (!pageCrumb) return;
    var session = state.activeSessionId ? findSession(state.activeSessionId) : null;
    pageCrumb.textContent = '';
    pageCrumb.appendChild(document.createTextNode('Chat '));
    var sep = document.createElement('span');
    sep.className = 'sep';
    sep.textContent = '/';
    pageCrumb.appendChild(sep);
    pageCrumb.appendChild(document.createTextNode(' '));
    var strong = document.createElement('strong');
    strong.textContent = session ? (session.name || 'Untitled') + ' · ' + session.id : 'New chat';
    pageCrumb.appendChild(strong);
  }

  /* options.force: show Chat without a session (the response gallery). */
  function setPage(page, options) {
    if (page === 'chat' && !state.activeSessionId && !(options && options.force)) page = 'home';
    if (!byId('page-' + page)) page = 'home';
    var changed = page !== currentPage;
    currentPage = page;
    pageViews.forEach(function (view) {
      var show = view.id === 'page-' + page;
      if (show && view.hidden && changed && !util.reducedMotion()) {
        view.classList.remove('is-entering');
        void view.offsetWidth;
        view.classList.add('is-entering');
      }
      view.hidden = !show;
    });
    pageNav.forEach(function (item) {
      var active = item.dataset.page === page;
      item.classList.toggle('active', active);
      if (active) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });
    if (page === 'chat') {
      renderChatCrumb();
    } else if (page === 'settings') {
      setSettingsSection(currentSettingsSection || 'profile');
    } else if (page === 'home') {
      if (pageCrumb) pageCrumb.textContent = 'Home';
      refreshHome();
    } else if (pageCrumb) {
      var nav = document.querySelector('.nav-item[data-page="' + page + '"]');
      pageCrumb.textContent = nav ? nav.textContent : page;
    }
    if (isMobile()) setDrawer(false);
    IM.bus.emit('page', page);
  }

  pageNav.forEach(function (item) {
    item.addEventListener('click', function () { setPage(item.dataset.page); });
  });

  function openSettings(section) {
    setPage('settings');
    setSettingsSection(section || currentSettingsSection);
  }

  document.addEventListener('click', function (event) {
    var target = event.target && event.target.closest ? event.target.closest('[data-home-goto]') : null;
    if (!target) return;
    var page = target.dataset.homeGoto;
    if (page === 'settings') openSettings(target.dataset.homeSection);
    else setPage(page);
  });

  /* ------------------------------------------------------------- sessions */

  function findSession(id) {
    return state.sessions.filter(function (item) { return item.id === id; })[0] || null;
  }

  function sessionTime(session) {
    return session.renamed_at || session.updated_at || session.created_at || 0;
  }

  function sortedSessions() {
    return state.sessions.slice().sort(function (a, b) {
      var ta = typeof sessionTime(a) === 'string' ? Date.parse(sessionTime(a)) / 1000 : sessionTime(a);
      var tb = typeof sessionTime(b) === 'string' ? Date.parse(sessionTime(b)) / 1000 : sessionTime(b);
      return (tb || 0) - (ta || 0);
    });
  }

  function setActiveSession(id) {
    state.activeSessionId = id || null;
    document.querySelectorAll('.session, .session-row').forEach(function (node) {
      var active = node.dataset.sessionId === state.activeSessionId;
      node.classList.toggle('active', active);
      if (active) node.setAttribute('aria-current', 'true');
      else node.removeAttribute('aria-current');
    });
    renderChatCrumb();
    IM.bus.emit('session.active', state.activeSessionId);
  }

  function renderSessions(sessions) {
    if (Array.isArray(sessions)) state.sessions = sessions;
    var sidebarList = byId('sidebar-sessions');
    var table = byId('sessions-table');
    var empty = byId('sessions-empty');
    var list = sortedSessions();
    var streamingId = IM.chat ? IM.chat.streamingSessionId() : null;

    if (sidebarList) {
      sidebarList.setAttribute('aria-busy', 'false');
      sidebarList.innerHTML = list.length ? list.map(function (s) {
        var active = s.id === state.activeSessionId;
        return '<li class="session' + (active ? ' active' : '') + (s.id === streamingId ? ' is-live' : '') + '"' +
          ' data-session-id="' + escapeHtml(s.id) + '" data-session-name="' + escapeHtml(s.name || '') + '"' +
          ' role="button" tabindex="0"' + (active ? ' aria-current="true"' : '') +
          ' title="' + escapeHtml((s.name || 'Untitled') + ' · ' + s.id) + '">' +
          '<span class="dot" aria-hidden="true"></span><span class="s-name">' + escapeHtml(s.name || 'Untitled') + '</span>' +
          '<span class="s-id">' + escapeHtml(s.id) + '</span>' +
          '<button class="s-delete" type="button" aria-label="Delete ' + escapeHtml(s.name || s.id) + '" title="Delete session">&#215;</button>' +
          '</li>';
      }).join('') : '<li class="sessions-empty">No sessions yet</li>';
    }

    if (table) {
      table.setAttribute('aria-busy', 'false');
      table.innerHTML = list.map(function (s) {
        var active = s.id === state.activeSessionId;
        return '<button class="session-row' + (active ? ' active' : '') + '" type="button"' +
          ' data-session-id="' + escapeHtml(s.id) + '" data-session-name="' + escapeHtml(s.name || '') + '"' +
          ' data-open-session="' + escapeHtml(s.id) + '">' +
          '<span class="dot" aria-hidden="true"></span>' +
          '<span class="session-main"><strong>' + escapeHtml(s.name || 'Untitled') + '</strong>' +
          '<span class="session-id">' + escapeHtml(s.id) + '</span></span>' +
          '<span class="session-meta">' + escapeHtml(util.formatTimeAgo(sessionTime(s))) + '</span>' +
          '<span class="chip ' + (active ? 'success' : 'neutral') + '">' + (s.id === streamingId ? 'replying' : (active ? 'open' : 'idle')) + '</span>' +
          '</button>';
      }).join('');
    }
    if (empty) empty.hidden = list.length > 0;
    renderHomeRecent();
    var count = byId('home-sessions');
    if (count) count.textContent = state.sessions.length + (state.sessions.length === 1 ? ' session' : ' sessions');
    IM.bus.emit('sessions', state.sessions);
  }

  function bindSessionLists() {
    var sidebarList = byId('sidebar-sessions');
    if (sidebarList) {
      sidebarList.addEventListener('click', function (event) {
        var row = event.target.closest('.session');
        if (!row) return;
        if (event.target.closest('.s-delete')) {
          event.stopPropagation();
          deleteSessionById(row.dataset.sessionId, row.dataset.sessionName);
          return;
        }
        activateSession(row.dataset.sessionId);
      });
      sidebarList.addEventListener('keydown', function (event) {
        var row = event.target.closest('.session');
        if (!row || event.target !== row) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          activateSession(row.dataset.sessionId);
        } else if (event.key === 'Delete') {
          event.preventDefault();
          deleteSessionById(row.dataset.sessionId, row.dataset.sessionName);
        } else if (event.key === 'F2') {
          event.preventDefault();
          openRename(row);
        } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          var sibling = event.key === 'ArrowDown' ? row.nextElementSibling : row.previousElementSibling;
          if (sibling && sibling.classList.contains('session')) sibling.focus();
        } else if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
          event.preventDefault();
          var rect = row.getBoundingClientRect();
          showSessionMenu({ preventDefault: function () {}, clientX: rect.left + 24, clientY: rect.bottom + 4 }, row);
        }
      });
      sidebarList.addEventListener('contextmenu', function (event) {
        var row = event.target.closest('.session');
        if (row) showSessionMenu(event, row);
      });
    }
    var table = byId('sessions-table');
    if (table) {
      table.addEventListener('click', function (event) {
        var row = event.target.closest('[data-open-session]');
        if (row) activateSession(row.dataset.openSession);
      });
      table.addEventListener('contextmenu', function (event) {
        var row = event.target.closest('.session-row');
        if (row) showSessionMenu(event, row);
      });
    }
  }

  async function activateSession(id) {
    if (!id) return;
    var same = id === state.activeSessionId;
    setActiveSession(id);
    setPage('chat');
    if (!same || !document.querySelector('#transcript .msg')) {
      if (IM.chat) await IM.chat.loadTranscript(id);
    }
    if (IM.chat && !isMobile()) IM.chat.focusComposer();
  }

  /* "New chat" opens an empty composer; the session itself is created (and
   * named from the message) by chat.js when the first message is sent, so
   * clicking New chat and walking away leaves nothing behind. */
  function newChat() {
    setActiveSession(null);
    setPage('chat', { force: true });  /* a draft has no session yet */
    if (IM.chat) IM.chat.clear();
    if (IM.chat && IM.chat.focusComposer) IM.chat.focusComposer();
  }

  async function createSession(firstCommand) {
    var text = String(firstCommand || '').trim();
    var session;
    try {
      session = await api.createSession(text || 'New session');
    } catch (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Session', title: 'Could not create the session', text: error.message });
      return null;
    }
    state.sessions = state.sessions.concat([session]);
    setActiveSession(session.id);
    renderSessions();
    setPage('chat');
    if (IM.chat) IM.chat.clear();
    return session;
  }

  async function deleteSessionById(id, name) {
    if (!id) return false;
    var label = name || id;
    if (IM.chat && IM.chat.streamingSessionId() === id) {
      toast({ tone: 'neutral', icon: 'clock', kicker: 'Delete', title: 'A reply is still running here', text: 'Stop it before deleting this session.' });
      return false;
    }
    if (!window.confirm('Delete "' + label + '"? This removes its transcript and memory.')) return false;
    try {
      await api.deleteSession(id);
    } catch (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Delete', title: 'Could not delete the session', text: error.message });
      return false;
    }
    state.sessions = state.sessions.filter(function (item) { return item.id !== id; });
    if (state.activeSessionId === id) {
      setActiveSession(null);
      if (IM.chat) IM.chat.clear();
      setPage('home');
    }
    renderSessions();
    renderChatCrumb();
    toast({ tone: 'neutral', icon: 'close', kicker: 'Deleted', title: 'Session deleted', text: label + ' and its transcript were removed.' });
    return true;
  }

  /* ----------------------------------------------- context menu, rename */

  var sessionMenu = byId('session-menu');
  var renameModal = byId('rename');
  var renameInput = byId('rename-input');
  var renameError = byId('rename-error');
  var menuSession = null;
  var renameSession = null;
  var releaseRename = null;
  var menuReturnFocus = null;

  function hideMenu(restoreFocus) {
    if (!sessionMenu || sessionMenu.hidden) return;
    sessionMenu.hidden = true;
    if (restoreFocus && menuReturnFocus && document.contains(menuReturnFocus)) menuReturnFocus.focus();
    menuReturnFocus = null;
  }

  function showSessionMenu(event, session) {
    if (!sessionMenu) return;
    event.preventDefault();
    menuSession = session;
    menuReturnFocus = document.activeElement;
    sessionMenu.hidden = false;
    var rect = sessionMenu.getBoundingClientRect();
    var left = Math.min(event.clientX, window.innerWidth - rect.width - 12);
    var top = Math.min(event.clientY, window.innerHeight - rect.height - 12);
    sessionMenu.style.left = Math.max(12, left) + 'px';
    sessionMenu.style.top = Math.max(12, top) + 'px';
    var item = sessionMenu.querySelector('[data-action="rename"]');
    if (item) item.focus();
  }

  if (sessionMenu) {
    sessionMenu.addEventListener('click', function (event) {
      var item = event.target.closest('[data-action]');
      if (!item) return;
      var target = menuSession;
      hideMenu(false);
      if (!target) return;
      if (item.dataset.action === 'rename') openRename(target);
      else if (item.dataset.action === 'delete') deleteSessionById(target.dataset.sessionId, target.dataset.sessionName);
    });
    sessionMenu.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      event.preventDefault();
      var items = Array.prototype.slice.call(sessionMenu.querySelectorAll('[role="menuitem"]'));
      var index = items.indexOf(document.activeElement);
      var next = items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length];
      if (next) next.focus();
    });
    document.addEventListener('click', function (event) {
      if (!sessionMenu.hidden && !sessionMenu.contains(event.target)) hideMenu(false);
    });
    document.addEventListener('scroll', function () { hideMenu(false); }, true);
    window.addEventListener('resize', function () { hideMenu(false); });
  }

  function openRename(session) {
    if (!renameModal || !session) return;
    renameSession = session;
    hideMenu(false);
    renameInput.value = session.dataset.sessionName || (findSession(session.dataset.sessionId) || {}).name || '';
    renameError.hidden = true;
    renameModal.hidden = false;
    releaseRename = util.trapFocus(renameModal);
    renameInput.focus();
    renameInput.select();
  }

  function closeRename() {
    if (renameModal) renameModal.hidden = true;
    renameSession = null;
    if (releaseRename) {
      releaseRename();
      releaseRename = null;
    }
  }

  function saveRename() {
    if (!renameSession) return;
    var value = renameInput.value.trim();
    if (!value) {
      renameError.textContent = 'A session name is required.';
      renameError.hidden = false;
      renameInput.focus();
      return;
    }
    var sessionId = renameSession.dataset.sessionId || '';
    var previous = findSession(sessionId);
    var oldName = previous ? previous.name : '';
    closeRename();
    if (!sessionId) return;
    /* Optimistic: show the new name now, roll back if the server refuses. */
    function rename(name) {
      state.sessions = state.sessions.map(function (item) {
        return item.id === sessionId ? Object.assign({}, item, { name: name }) : item;
      });
      renderSessions();
      if (state.activeSessionId === sessionId) renderChatCrumb();
    }
    rename(value);
    api.updateSession(sessionId, { name: value }).then(function () {
      toast({ tone: 'success', icon: 'pencil', kicker: 'Renamed', title: 'Session renamed',
        text: oldName === value ? 'The name was already up to date.' : 'Now ' + value + ' · ' + sessionId + '.' });
    }, function (error) {
      rename(oldName);
      toast({ tone: 'error', icon: 'alert', kicker: 'Rename', title: 'Rename not saved', text: error.message });
    });
  }

  if (renameModal) {
    byId('rename-cancel').addEventListener('click', closeRename);
    byId('rename-save').addEventListener('click', saveRename);
    renameInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        saveRename();
      }
    });
    renameModal.addEventListener('click', function (event) {
      if (event.target === renameModal) closeRename();
    });
  }

  /* Global Escape: innermost surface first, then the live reply. */
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (renameModal && !renameModal.hidden) { closeRename(); event.preventDefault(); return; }
    if (sessionMenu && !sessionMenu.hidden) { hideMenu(true); event.preventDefault(); return; }
    if (shell && shell.classList.contains('nav-open')) { setDrawer(false); event.preventDefault(); return; }
    if (IM.toast && IM.toast.hasApproval()) { IM.toast.cancelApproval(); event.preventDefault(); return; }
    if (IM.chat && IM.chat.isStreaming() && currentPage === 'chat') { IM.chat.stop(); event.preventDefault(); }
  });

  function handleCommand(text) {
    var value = String(text || '').trim();
    if (value !== '/rename' && value !== '/rename-session') return false;
    var row = document.querySelector('.session.active');
    if (!row) {
      toast({ tone: 'neutral', kicker: 'Rename', title: 'No active session', text: 'Start a session first.' });
      return true;
    }
    openRename(row);
    return true;
  }

  /* ----------------------------------------------------------------- home */

  function renderHomeRecent() {
    var list = byId('home-recent-list');
    var empty = byId('home-recent-empty');
    if (!list || !empty) return;
    list.setAttribute('aria-busy', 'false');
    var recent = sortedSessions().slice(0, 5);
    empty.hidden = recent.length > 0;
    list.innerHTML = recent.map(function (session) {
      return '<button class="home-recent-row" type="button" data-open-session="' + escapeHtml(session.id) + '">' +
        '<span class="dot" aria-hidden="true"></span><span class="home-recent-main">' +
        '<strong>' + escapeHtml(session.name || 'Untitled session') + '</strong>' +
        '<span>' + escapeHtml(util.formatTimeAgo(sessionTime(session))) + '</span>' +
        '</span></button>';
    }).join('');
  }

  var homeRecent = byId('home-recent-list');
  if (homeRecent) {
    homeRecent.addEventListener('click', function (event) {
      var row = event.target.closest('[data-open-session]');
      if (row) activateSession(row.dataset.openSession);
    });
  }

  function refreshHome() {
    renderHomeRecent();
    if (IM.status) IM.status.refresh();
  }

  /* Status line and home status, from the shared status store. */
  function renderStatus(status) {
    var engineState = IM.status.engineState(status);
    var dot = byId('home-engine-dot');
    var engine = byId('home-engine');
    var model = byId('home-model');
    var statusText = byId('status-text');
    var statusPulse = byId('status-pulse');
    var engineName = (status && status.engine && status.engine.engine) || '';
    var engineLabel = engineName === 'fish' ? 'FISH' : (engineName === 'local' ? 'Local core' : engineName);
    var labels = {
      ready: engineLabel ? 'Engine online · ' + engineLabel : 'Engine online',
      provisioning: 'Engine provisioning',
      unreachable: 'Server unreachable'
    };
    if (dot) dot.className = 'dot ' + (engineState === 'ready' ? 'is-online' : (engineState === 'provisioning' ? 'is-pending' : 'is-offline'));
    var homeLabels = {
      ready: engineLabel ? 'Online · ' + engineLabel : 'Online',
      provisioning: 'Starting up, or not installed yet',
      unreachable: 'Server unreachable'
    };
    if (engine) engine.textContent = homeLabels[engineState];
    var eventsNote = state.events === 'offline' ? ' · live updates off' : (state.events === 'reconnecting' ? ' · reconnecting' : '');
    if (statusText) statusText.textContent = labels[engineState].toLowerCase() + eventsNote;
    if (statusPulse) {
      statusPulse.classList.toggle('is-online', engineState === 'ready');
      statusPulse.classList.toggle('is-pending', engineState === 'provisioning');
      statusPulse.classList.toggle('is-offline', engineState === 'unreachable');
    }
    var statusRow = document.querySelector('.side-foot .status');
    if (statusRow) statusRow.dataset.state = engineState;
    var modelDot = byId('home-model-dot');
    if (modelDot) {
      modelDot.className = 'dot ' + (status && status.selected_model ? 'is-online'
        : (state.modelsDownloaded > 0 ? 'is-pending' : 'is-offline'));
    }
    if (model) {
      if (status && status.selected_model) model.textContent = status.selected_model;
      else if (state.modelsDownloaded === 0) model.textContent = 'No model downloaded';
      else if (state.modelsDownloaded > 0) model.textContent = 'Model not loaded yet';
      else model.textContent = 'Model status unknown';
    }
    document.body.dataset.engine = engineState;
  }

  IM.bus.on('status', renderStatus);
  IM.bus.on('models', function () { renderStatus(state.status); });

  function bindHome() {
    var greeting = byId('home-greeting');
    if (greeting) {
      var hour = new Date().getHours();
      greeting.textContent = hour < 5 ? 'Working late' : hour < 12 ? 'Good morning'
        : hour < 18 ? 'Good afternoon' : 'Good evening';
    }
    var quick = byId('home-quick');
    var homePrompt = byId('home-prompt');
    if (quick && homePrompt) {
      quick.addEventListener('submit', async function (event) {
        event.preventDefault();
        var value = homePrompt.value.trim();
        if (!value) {
          homePrompt.focus();
          return;
        }
        var button = quick.querySelector('.home-send');
        if (button) {
          button.disabled = true;
          button.textContent = 'Starting…';
        }
        var session = await createSession(value);
        if (button) {
          button.disabled = false;
          button.textContent = 'Start chat';
        }
        if (session) {
          homePrompt.value = '';
          if (IM.chat) IM.chat.send(value, session.id);
        }
      });
      homePrompt.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          if (quick.requestSubmit) quick.requestSubmit();
          else quick.dispatchEvent(new Event('submit', { cancelable: true }));
        }
      });
    }

    document.querySelectorAll('[data-home-prompt]').forEach(function (chip) {
      chip.addEventListener('click', function () {
        if (!homePrompt) return;
        homePrompt.value = chip.dataset.homePrompt || '';
        homePrompt.focus();
      });
    });

    ['home-new-session', 'new-session', 'new-session-empty', 'sidebar-new-chat'].forEach(function (id) {
      var button = byId(id);
      if (button) button.addEventListener('click', newChat);
    });
  }

  /* ----------------------------------------------------------- connectors */

  var CONNECTOR_ICONS = {
    gmail: '<svg viewBox="0 0 24 24"><rect x="2.8" y="5.2" width="18.4" height="13.6" rx="2.2" fill="#fff" stroke="#EA4335" stroke-width="1.5"/><path d="M4.2 7.2L12 12.6l7.8-5.4" fill="none" stroke="#EA4335" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    github: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 .8a11.2 11.2 0 0 0-3.54 21.82c.56.1.77-.24.77-.54v-2.1c-3.13.68-3.79-1.33-3.79-1.33-.51-1.3-1.25-1.64-1.25-1.64-1.02-.7.08-.68.08-.68 1.13.08 1.73 1.16 1.73 1.16 1 1.72 2.63 1.22 3.27.93.1-.72.39-1.22.71-1.5-2.5-.28-5.13-1.25-5.13-5.56 0-1.23.44-2.24 1.16-3.03-.12-.28-.5-1.42.11-2.96 0 0 .95-.3 3.1 1.15a10.8 10.8 0 0 1 5.64 0c2.15-1.45 3.1-1.15 3.1-1.15.61 1.54.23 2.68.11 2.96.72.79 1.16 1.8 1.16 3.03 0 4.32-2.64 5.28-5.15 5.55.4.35.76 1.04.76 2.1v3.12c0 .3.2.65.78.54A11.2 11.2 0 0 0 12 .8z"/></svg>',
    slack: '<svg viewBox="0 0 24 24"><path fill="#E01E5A" d="M9.4 2a2.1 2.1 0 0 0 0 4.2h2.1V4.1A2.1 2.1 0 0 0 9.4 2zm0 5.6H3.7a2.1 2.1 0 0 0 0 4.2h5.7a2.1 2.1 0 0 0 0-4.2z"/><path fill="#36C5F0" d="M22 9.6a2.1 2.1 0 0 0-4.2 0v2.1h2.1A2.1 2.1 0 0 0 22 9.6zm-5.6 0V3.9a2.1 2.1 0 0 0-4.2 0v5.7a2.1 2.1 0 0 0 4.2 0z"/><path fill="#2EB67D" d="M14.6 22a2.1 2.1 0 0 0 0-4.2h-2.1v2.1A2.1 2.1 0 0 0 14.6 22zm0-5.6h5.7a2.1 2.1 0 0 0 0-4.2h-5.7a2.1 2.1 0 0 0 0 4.2z"/><path fill="#ECB22E" d="M2 14.4a2.1 2.1 0 0 0 4.2 0v-2.1H4.1A2.1 2.1 0 0 0 2 14.4zm5.6 0v5.7a2.1 2.1 0 0 0 4.2 0v-5.7a2.1 2.1 0 0 0-4.2 0z"/></svg>',
    vercel: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3.2l9.6 17.2H2.4L12 3.2z"/></svg>',
    render: '<svg viewBox="0 0 24 24" fill="none" stroke="#7C3AED" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17.8a4 4 0 0 1-.3-8 5.4 5.4 0 0 1 10.4 1.1 3.5 3.5 0 0 1-.6 6.9H7z"/><path d="M12 14.2v5.2M9.9 16.3L12 14.1l2.1 2.2"/></svg>',
    cloudflare: '<svg viewBox="0 0 24 24" fill="#F48120"><path d="M18.4 10.1a5.4 5.4 0 0 0-10.3-1.3A4.3 4.3 0 0 0 8.5 17h9.2a3.4 3.4 0 0 0 .7-6.9z"/><path d="M16.6 17h-2.1l.4-2.2h1.7a1.1 1.1 0 0 1 0 2.2z" fill="#FBAD41"/></svg>',
    supabase: '<svg viewBox="0 0 24 24" fill="#3ECF8E"><path d="M13.4 2.1L4.2 13.3h6.1L10.6 21.9l9.2-11.2h-6.1l-.3-8.6z"/></svg>',
    canva: '<svg viewBox="0 0 24 24"><defs><linearGradient id="canva-mark" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#00C4CC"/><stop offset="1" stop-color="#7D2AE8"/></linearGradient></defs><circle cx="12" cy="12" r="10" fill="url(#canva-mark)"/><path d="M15.8 8.1a4.5 4.5 0 1 0 0 7.8" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></svg>'
  };

  var CONNECTOR_COPY = {
    gmail: 'Read threads and prepare drafts. Sending opens an editable approval card.',
    github: 'Read repositories and issues. Opening an issue or commenting asks first.',
    slack: 'Search channels and read history. Posting asks before every message.',
    vercel: 'List projects and deployments. Creating a deployment asks first.',
    render: 'List services and deploys. Triggering a deploy or restart asks first.',
    cloudflare: 'Read zones and DNS. Changing DNS or purging cache asks first.',
    supabase: 'List projects and organizations. Creating a project asks first.',
    canva: 'List designs and export them. Creating a design asks first.'
  };

  function renderConnectorGrid(connectors) {
    var grid = byId('connector-grid');
    if (!grid) return;
    grid.setAttribute('aria-busy', 'false');
    if (!connectors.length) {
      grid.innerHTML = '<div class="empty-state"><h3>No connectors reported</h3><p>The server did not list any connectors.</p></div>';
      return;
    }
    grid.innerHTML = connectors.map(function (c) {
      var id = String(c.id || '');
      var statusText = c.connected ? 'Connected' : (c.error ? 'Needs auth' : 'Not connected');
      var statusClass = c.connected ? 'success' : (c.error ? 'error' : 'neutral');
      var account = c.account || (c.connected ? 'Connected' : 'No account');
      return '<article class="panel connector" data-connector="' + escapeHtml(id) + '">' +
        '<div class="connector-head">' +
          '<span class="connector-mark connector-mark--' + escapeHtml(id) + '" aria-hidden="true">' + (CONNECTOR_ICONS[id] || '') + '</span>' +
          '<div class="connector-name"><strong>' + escapeHtml(c.label || id) + '</strong></div>' +
          '<span class="chip ' + statusClass + '" data-status>' + statusText + '</span>' +
        '</div>' +
        '<p>' + escapeHtml(CONNECTOR_COPY[id] || c.description || '') + '</p>' +
        '<div class="connector-foot">' +
          '<span class="muted" data-account>' + escapeHtml(account) + '</span>' +
          '<button class="btn ghost" type="button" data-conn-setup="' + escapeHtml(id) + '" title="OAuth app, tools and set-up steps">Set up</button>' +
          '<button class="' + (c.connected ? 'btn' : 'btn primary') + '" type="button" ' +
            (c.connected ? 'data-disconnect' : 'data-connect') + '="' + escapeHtml(id) + '">' + (c.connected ? 'Disconnect' : 'Connect') + '</button>' +
        '</div>' +
      '</article>';
    }).join('');
  }

  function updateConnectorUI(card, connected, account) {
    var chip = card.querySelector('[data-status]');
    var btn = card.querySelector('[data-connect], [data-disconnect]');
    var accountEl = card.querySelector('[data-account]');
    if (chip) {
      chip.textContent = connected ? 'Connected' : 'Not connected';
      chip.className = 'chip ' + (connected ? 'success' : 'neutral');
    }
    if (btn) {
      btn.textContent = connected ? 'Disconnect' : 'Connect';
      btn.disabled = false;
      if (connected) {
        btn.setAttribute('data-disconnect', card.dataset.connector);
        btn.removeAttribute('data-connect');
        btn.className = 'btn';
      } else {
        btn.setAttribute('data-connect', card.dataset.connector);
        btn.removeAttribute('data-disconnect');
        btn.className = 'btn primary';
      }
    }
    if (accountEl) accountEl.textContent = account || (connected ? 'Connected' : 'No account');
  }

  /* OAuth: open the provider in a popup; finish on the popup's postMessage
   * or when it closes. Both paths remove their listener and timer, and the
   * "Complete authorization" card is dismissed either way. */
  async function handleConnectorConnect(card) {
    var id = card.dataset.connector;
    var btn = card.querySelector('[data-connect]');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = 'Connecting…';
    var toastId = 'oauth-' + id;
    var origin = await api.apiOrigin();
    var finished = false;
    var poll = null;
    var popup = null;

    function finish(connected, account, errorText) {
      if (finished) return;
      finished = true;
      if (poll) clearInterval(poll);
      window.removeEventListener('message', onMessage);
      if (IM.toast) IM.toast.dismiss(toastId);
      updateConnectorUI(card, connected, account);
      if (connected) {
        toast({ tone: 'success', icon: 'check', kicker: 'Connected', title: id + ' connected', text: 'The integration is ready. Tokens stay local.' });
      } else {
        toast({ tone: 'error', icon: 'alert', kicker: 'Not connected', title: id + ' was not connected', text: errorText || 'The authorization was not completed.' });
      }
    }

    function onMessage(event) {
      if (event.origin !== origin && event.origin !== location.origin) return;
      var data = event.data || {};
      if (data.type !== 'oauth_callback' || data.connector !== id) return;
      finish(Boolean(data.success), data.account, data.error);
    }

    try {
      /* The server opens the system browser (Google refuses sign-in inside
       * an embedded webview); the `connector` frame finishes the card. */
      var result = await api.connectConnector(id, { open: true });
      if (!result.opened) popup = window.open(result.url, 'oauth', 'width=600,height=700');
      if (!result.opened && !popup) throw new Error('The browser could not be opened. Use Set up > Connect, which shows the sign-in link.');
      window.addEventListener('message', onMessage);
      toast({ id: toastId, tone: 'neutral', icon: 'clock', kicker: 'OAuth', title: 'Complete authorization', text: 'Finish signing in to ' + id + ' in your browser.', timeout: 0 });
      var startedAt = Date.now();
      poll = setInterval(async function () {
        if (finished) return;
        if (!popup) {
          /* system browser: follow the status (the `connector` frame also
           * updates the card); give up with the server's 10-minute state */
          try {
            var now = await api.getConnectorStatus(id);
            if (now && now.connected) finish(true, now.account);
            else if (Date.now() - startedAt > 600000) finish(false, null, 'The sign-in was not finished within 10 minutes.');
          } catch (error) { /* keep waiting */ }
          return;
        }
        if (!popup.closed) return;
        clearInterval(poll);
        poll = null;
        try {
          var status = await api.getConnectorStatus(id);
          finish(Boolean(status.connected), status.account);
        } catch (error) {
          finish(false, null, 'Could not verify the connection: ' + error.message);
        }
      }, 1000);
    } catch (error) {
      finished = true;
      window.removeEventListener('message', onMessage);
      btn.disabled = false;
      btn.textContent = 'Connect';
      toast({ tone: 'error', icon: 'alert', kicker: 'OAuth', title: 'Could not start sign-in', text: error.message });
    }
  }

  async function handleConnectorDisconnect(card) {
    var id = card.dataset.connector;
    var btn = card.querySelector('[data-disconnect]');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = 'Disconnecting…';
    try {
      await api.disconnectConnector(id);
      updateConnectorUI(card, false, null);
      toast({ tone: 'neutral', icon: 'info', kicker: 'Disconnected', title: id + ' disconnected', text: 'The local token was removed.' });
    } catch (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Disconnect', title: 'Could not disconnect', text: error.message });
      btn.disabled = false;
      btn.textContent = 'Disconnect';
    }
  }

  var connectorGrid = byId('connector-grid');
  if (connectorGrid) {
    connectorGrid.addEventListener('click', function (event) {
      var setup = event.target.closest('[data-conn-setup]');
      if (setup) {
        openSettings('connectors');
        if (IM.connectors) IM.connectors.focus(setup.getAttribute('data-conn-setup'));
        return;
      }
      var button = event.target.closest('[data-connect], [data-disconnect]');
      if (!button) return;
      var card = button.closest('.connector');
      if (!card) return;
      if (button.hasAttribute('data-connect')) handleConnectorConnect(card);
      else handleConnectorDisconnect(card);
    });
  }

  /* --------------------------------------------------------------- skills */

  /* The Skills page and the Add skill dialog live in skills.js, which loads
   * after this file; the first list may arrive before it does. */
  var pendingSkills = null;
  function renderSkills(skills) {
    if (IM.skills && IM.skills.render) IM.skills.render(skills);
    else pendingSkills = skills;
  }
  IM.takePendingSkills = function () {
    var value = pendingSkills;
    pendingSkills = null;
    return value;
  };

  /* -------------------------------------------------------------- persona */

  /* The server stores {name, email_role, description}. The form's Tone maps
   * to email_role and System prompt to description; tone/prompt are sent
   * too so a newer server can store them under their own names. */
  async function loadPersona() {
    var nameInput = byId('persona-name');
    var toneInput = byId('persona-tone');
    var promptInput = byId('persona-prompt');
    try {
      var persona = await api.getPersona();
      if (!persona || !persona.configured) return;
      if (nameInput && persona.name) nameInput.value = persona.name;
      var tone = persona.tone || persona.email_role;
      if (toneInput && tone) {
        var exists = Array.prototype.some.call(toneInput.options, function (option) { return option.value === tone; });
        if (!exists) {
          var option = document.createElement('option');
          option.textContent = tone;
          toneInput.appendChild(option);
        }
        toneInput.value = tone;
      }
      if (promptInput && (persona.prompt || persona.description)) promptInput.value = persona.prompt || persona.description;
    } catch (error) { /* the defaults in the form stay */ }
  }

  var savePersonaButton = byId('save-persona');
  if (savePersonaButton) {
    savePersonaButton.addEventListener('click', async function () {
      var name = (byId('persona-name') || {}).value || 'Imurun';
      var tone = (byId('persona-tone') || {}).value || '';
      var systemPrompt = (byId('persona-prompt') || {}).value || '';
      savePersonaButton.disabled = true;
      try {
        await api.savePersona({ name: name.trim(), email_role: tone, description: systemPrompt, tone: tone, prompt: systemPrompt });
        toast({ tone: 'success', icon: 'check', kicker: 'Persona saved', title: (name.trim() || 'Imurun') + ' updated', text: 'New turns will use "' + tone + '".' });
      } catch (error) {
        toast({ tone: 'error', icon: 'alert', kicker: 'Persona', title: 'Could not save the persona', text: error.message });
      } finally {
        savePersonaButton.disabled = false;
      }
    });
  }

  /* ---------------------------------------------------- settings actions */

  function on(selector, handler) {
    document.querySelectorAll(selector).forEach(function (node) { node.addEventListener('click', handler); });
  }

  on('[data-api-action="copy-endpoint"]', function () {
    var endpoint = byId('api-endpoint').textContent;
    if (IM.chat) IM.chat.copyText(endpoint, this, 'Copy endpoint');
  });

  on('[data-api-action="rotate-token"]', async function () {
    var btn = this;
    if (!window.confirm('Rotate the bearer token? Other clients using the old token will stop working.')) return;
    btn.disabled = true;
    btn.textContent = 'Rotating…';
    try {
      var result = await api.rotateToken();
      if (result && result.token) {
        api.setToken(result.token);
        byId('api-token').textContent = result.token.slice(0, 4) + '…' + result.token.slice(-4);
        /* The event stream authenticated with the old token. */
        if (window.__imurun_eventsource) window.__imurun_eventsource.reconnect();
      }
      toast({ tone: 'success', icon: 'check', kicker: 'Rotated', title: 'Bearer token rotated', text: 'This window now uses the new token.' });
    } catch (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Token', title: 'Could not rotate the token', text: error.message });
    } finally {
      btn.disabled = false;
      btn.textContent = 'Rotate token';
    }
  });

  /* The server's clear deletes downloaded model weights (*.gguf). The old
   * confirm text promised models were kept; it now says what happens. */
  on('[data-resources-action="clear-cache"]', async function () {
    if (!window.confirm('Clear the model cache? This deletes downloaded model files (*.gguf) from the models folder. Chat history is kept.')) return;
    var btn = this;
    btn.disabled = true;
    btn.textContent = 'Clearing…';
    try {
      var result = await api.clearCache();
      var freed = result && result.removed_bytes ? util.formatBytes(result.removed_bytes) : null;
      toast({ tone: 'success', icon: 'check', kicker: 'Cleared', title: 'Model cache cleared', text: freed ? freed + ' freed. Download a model again to chat.' : 'Download a model again to chat.' });
      refreshResources();
      if (IM.models) IM.models.refresh();
    } catch (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Resources', title: 'Could not clear the cache', text: error.message });
    } finally {
      btn.disabled = false;
      btn.textContent = 'Clear cache';
    }
  });

  on('[data-resources-action="refresh"]', function () { refreshResources(true); });

  var stackButton = byId('build-start-stack');
  if (stackButton) {
    stackButton.addEventListener('click', async function () {
      var btn = this;
      var label = btn.innerHTML;
      btn.disabled = true;
      btn.textContent = 'Building…';
      toast({ id: 'stack', tone: 'neutral', icon: 'spinner', kicker: 'Stack', title: 'Building Docker images', text: 'This can take several minutes on the first run.', timeout: 0 });
      try {
        await api.stackBuild();
        btn.textContent = 'Starting…';
        toast({ id: 'stack', tone: 'neutral', icon: 'spinner', kicker: 'Stack', title: 'Starting services', text: 'Images built.', timeout: 0 });
        await api.stackStart();
        toast({ id: 'stack', tone: 'success', icon: 'check', kicker: 'Stack', title: 'Full stack started', text: 'The engine may need a moment to load the model.', timeout: 6000 });
        refreshStack();
      } catch (error) {
        toast({ id: 'stack', tone: 'error', icon: 'alert', kicker: 'Stack', title: 'Stack operation failed', text: error.message, timeout: 9000 });
      } finally {
        btn.disabled = false;
        btn.innerHTML = label;
        if (IM.status) IM.status.refresh();
      }
    });
  }

  async function refreshStack() {
    var endpoint = byId('fish-endpoint');
    var chip = byId('fish-status');
    try {
      var stack = await api.stackStatus();
      if (endpoint) endpoint.textContent = stack.running + ' of ' + stack.total + ' services running' + (stack.docker ? '' : ' (Docker not found)');
      if (chip) {
        chip.textContent = stack.fish_healthy ? 'healthy' : (stack.running ? 'starting' : 'stopped');
        chip.className = 'chip ' + (stack.fish_healthy ? 'success' : 'neutral');
      }
    } catch (error) {
      if (endpoint) endpoint.textContent = 'Stack status unavailable';
      if (chip) {
        chip.textContent = 'unknown';
        chip.className = 'chip neutral';
      }
    }
  }

  function resourceSizeLabel(value) {
    if (value && typeof value === 'object') return util.formatBytes(Number(value.bytes) || 0);
    if (typeof value === 'number') return util.formatBytes(value);
    if (typeof value === 'string' && value) return value;
    return '—';
  }

  function meter(prefix, percent, text) {
    var pct = byId(prefix + '-pct');
    var bar = byId(prefix + '-bar');
    var label = byId(prefix + '-text');
    var known = typeof percent === 'number' && isFinite(percent);
    if (pct) pct.textContent = known ? Math.round(percent) + '%' : '—';
    if (bar) {
      bar.style.width = (known ? Math.max(0, Math.min(100, percent)) : 0) + '%';
      bar.parentNode.classList.toggle('is-high', known && percent >= 85);
    }
    if (label) label.textContent = text;
  }

  function renderResources(data) {
    if (!data) return;
    var gb = function (bytes, digits) { return (Number(bytes) / 1e9).toFixed(digits) + ' GB'; };
    if (data.memory) meter('mem', data.memory.percent, gb(data.memory.used, 1) + ' of ' + gb(data.memory.total, 1));
    else meter('mem', null, 'Not reported on this system');
    if (data.disk) meter('disk', data.disk.percent, gb(data.disk.used, 0) + ' of ' + gb(data.disk.total, 0));
    var cpu = data.cpu && typeof data.cpu.percent === 'number' ? data.cpu.percent : null;
    meter('cpu', cpu, cpu === null ? 'Not reported on this system' : 'Current load');
    var cache = byId('cache-size');
    var transcripts = byId('transcripts-size');
    var logs = byId('logs-size');
    if (cache) cache.textContent = resourceSizeLabel(data.cache);
    if (transcripts) transcripts.textContent = resourceSizeLabel(data.transcripts);
    if (logs) logs.textContent = resourceSizeLabel(data.logs);
  }

  function refreshResources(manual) {
    return api.getResources().then(renderResources).catch(function (error) {
      if (manual) toast({ tone: 'error', icon: 'alert', kicker: 'Resources', title: 'Could not refresh resources', text: error.message });
    });
  }

  /* -------------------------------------------------------------- clients */

  var clientsTable = byId('clients-table');
  var clientsHeader = clientsTable ? clientsTable.querySelector('.settings-table-row') : null;
  var clientsEmpty = byId('clients-empty');

  function renderClients(clients) {
    if (!clientsTable) return;
    clientsTable.innerHTML = '';
    if (clientsHeader) clientsTable.appendChild(clientsHeader);
    if (!clients.length) {
      if (clientsEmpty) clientsTable.appendChild(clientsEmpty);
      return;
    }
    clients.forEach(addClientRow);
  }

  function addClientRow(client) {
    if (!clientsTable) return;
    if (clientsEmpty && clientsEmpty.parentNode) clientsEmpty.remove();
    var row = document.createElement('div');
    row.className = 'settings-table-row';
    row.dataset.clientId = client.id;
    var scopes = Array.isArray(client.scopes) ? client.scopes.join(', ') : (client.scope || '');
    var status = client.status
      ? '<span class="chip ' + (client.status === 'active' ? 'success' : 'neutral') + '">' + escapeHtml(client.status) + '</span>'
      : '<span class="muted">—</span>';
    row.innerHTML = '<span>' + escapeHtml(client.name || client.id || 'Unnamed client') + '</span>' +
      '<span class="mono">' + escapeHtml(scopes || '—') + '</span>' + status +
      '<span><button class="btn ghost" type="button" data-clients-action="revoke" data-client-id="' + escapeHtml(client.id) + '">Revoke</button></span>';
    clientsTable.appendChild(row);
  }

  async function addClient() {
    var name = window.prompt('Client name:');
    if (!name || !name.trim()) return;
    var scope = window.prompt('Scopes, comma-separated (chat, sessions, skills, models, connectors, read-only, admin):', 'read-only') || 'read-only';
    try {
      var client = await api.createClient({
        name: name.trim(),
        scopes: scope.split(',').map(function (s) { return s.trim(); }).filter(Boolean)
      });
      addClientRow(client);
      toast({ tone: 'success', icon: 'check', kicker: 'Client added', title: name.trim(),
        text: client.token ? 'Copy the token now, it is shown once: ' + client.token : 'Scopes: ' + scope, timeout: client.token ? 0 : undefined });
    } catch (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Clients', title: 'Could not add the client', text: error.message });
    }
  }

  async function revokeClient(id) {
    if (!window.confirm('Revoke this client? Its token stops working immediately.')) return;
    try {
      await api.revokeClient(id);
      var row = clientsTable && clientsTable.querySelector('[data-client-id="' + id + '"]');
      if (row) row.remove();
      if (clientsTable && clientsTable.querySelectorAll('[data-client-id]').length === 0 && clientsEmpty) clientsTable.appendChild(clientsEmpty);
      toast({ tone: 'neutral', icon: 'info', kicker: 'Revoked', title: 'Client revoked', text: 'Its token is no longer accepted.' });
    } catch (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Clients', title: 'Could not revoke the client', text: error.message });
    }
  }

  /* One listener covers the header button, the empty-state button and every
   * row's Revoke button (the empty-state button used to be dead). */
  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('[data-clients-action]') : null;
    if (!button) return;
    var action = button.dataset.clientsAction;
    if (action === 'add') addClient();
    else if (action === 'revoke') revokeClient(button.dataset.clientId);
    else if (action === 'refresh') {
      api.listClients().then(renderClients, function (error) {
        toast({ tone: 'error', icon: 'alert', kicker: 'Clients', title: 'Could not load clients', text: error.message });
      });
    }
  });

  /* ---------------------------------------------------------------- usage */

  function updateUsageUI(data) {
    if (!data || typeof data !== 'object') return;
    var historyLabels = Array.isArray(data.historyLabels) ? data.historyLabels : [];
    var historyTokens = Array.isArray(data.historyTokens) ? data.historyTokens.map(function (v) { return Number(v) || 0; }) : [];
    var turns = byId('usage-turns');
    var tokens = byId('usage-tokens');
    var wait = byId('usage-wait');
    var detail = byId('usage-turns-detail');
    var chart = byId('usage-chart');
    var labels = byId('usage-labels');
    if (turns) turns.textContent = data.turnsToday || 0;
    if (tokens) tokens.textContent = util.formatTokens(data.tokensToday || 0);
    if (wait) wait.textContent = (Number(data.queueWaitP50) || 0) + 's';
    if (detail) detail.textContent = (data.buildTurns || 0) + ' build · ' + (data.planTurns || 0) + ' plan';
    if (chart) {
      var peak = historyTokens.reduce(function (max, v) { return Math.max(max, v); }, 0);
      chart.innerHTML = historyTokens.map(function (value, index) {
        var height = peak > 0 ? Math.max(3, Math.round((value / peak) * 100)) : 3;
        return '<span class="' + (value === 0 ? 'is-empty' : '') + '" style="height:' + height + '%"' +
          ' title="' + escapeHtml((historyLabels[index] || '') + ' · ' + util.formatTokens(value) + ' tokens') + '"></span>';
      }).join('');
      chart.setAttribute('role', 'img');
    }
    if (labels) {
      var days = historyLabels.length ? historyLabels : ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
      labels.innerHTML = days.map(function (label) { return '<span>' + escapeHtml(label) + '</span>'; }).join('');
    }
  }

  function exportUsageCSV(data) {
    var labels = Array.isArray(data && data.historyLabels) ? data.historyLabels : [];
    var turns = Array.isArray(data && data.historyTurns) ? data.historyTurns : [];
    var tokens = Array.isArray(data && data.historyTokens) ? data.historyTokens : [];
    var csv = 'Date,Turns,Tokens\n' + labels.map(function (label, index) {
      return '"' + String(label).replace(/"/g, '""') + '",' + (Number(turns[index]) || 0) + ',' + (Number(tokens[index]) || 0);
    }).join('\n') + '\n';
    var url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    var a = document.createElement('a');
    a.href = url;
    a.download = 'imurun-usage-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    /* Revoking synchronously can cancel the download in some browsers. */
    setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
    toast({ tone: 'success', icon: 'check', kicker: 'Exported', title: 'Usage CSV downloaded', text: a.download });
  }

  on('[data-usage-action="export"]', function () {
    api.getUsage().then(exportUsageCSV, function (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Usage', title: 'Could not export usage', text: error.message });
    });
  });
  on('[data-usage-action="refresh"]', function () {
    api.getUsage().then(updateUsageUI, function (error) {
      toast({ tone: 'error', icon: 'alert', kicker: 'Usage', title: 'Could not load usage', text: error.message });
    });
  });

  /* ---------------------------------------------------------- server events */

  function handleServerEvent(event) {
    IM.bus.emit('server:' + event.type, event);
    switch (event.type) {
      case 'connector': {
        var card = document.querySelector('[data-connector="' + String(event.connector || '').replace(/"/g, '') + '"]');
        if (card) updateConnectorUI(card, event.connected, event.account);
        /* One notice per connector (id shared with connectors-settings.js),
         * and a failed or cancelled sign-in is not a "disconnect". */
        var failedSignIn = !event.connected && event.via === 'oauth';
        toast({
          id: 'connector-' + String(event.connector || ''),
          tone: event.connected ? 'success' : (failedSignIn ? 'error' : 'neutral'), icon: event.connected ? 'check' : (failedSignIn ? 'alert' : 'info'),
          kicker: event.connected ? 'Connected' : (failedSignIn ? 'Not connected' : 'Disconnected'),
          title: (event.connector || 'Connector') + (event.connected ? ' connected' : (failedSignIn ? ' was not connected' : ' disconnected')),
          text: event.message || ''
        });
        break;
      }
      case 'session':
        if (event.action === 'created' || event.action === 'renamed' || event.action === 'deleted') {
          api.listSessions().then(renderSessions, function () {});
        }
        break;
      case 'usage':
        /* The frame IS the usage view ({turnsToday, ...}); it was read as
         * event.data before, which is undefined, so the panel never updated. */
        updateUsageUI(event.data && typeof event.data === 'object' ? event.data : event);
        break;
      case 'compaction.start':
        toast({ id: 'compaction', tone: 'neutral', icon: 'spinner', kicker: 'Context', title: 'Compacting the conversation',
          text: (event.original_messages || 0) + ' messages, aiming for about ' + util.formatTokens(event.target_tokens || 0) + ' tokens.', timeout: 0 });
        break;
      case 'compaction.progress':
        toast({ id: 'compaction', tone: 'neutral', icon: 'spinner', kicker: 'Context', title: 'Compacting the conversation',
          text: event.stage === 'summarizing'
            ? 'Summarising ' + (event.messages_to_summarize || 0) + ' older messages…'
            : 'Rebuilding the context with the summary…', timeout: 0 });
        break;
      case 'compaction.done':
        toast({ id: 'compaction', tone: 'success', icon: 'check', kicker: 'Context', title: 'Conversation compacted',
          text: (event.original_messages || 0) + ' → ' + (event.compacted_messages || 0) + ' messages (about ' + util.formatTokens(event.estimated_tokens || 0) + ' tokens).', timeout: 4500 });
        break;
      case 'web_search.start':
      case 'web_search.progress':
      case 'web_search.done':
        if (IM.chat) IM.chat.onServerEvent(event);
        break;
      case 'turn.start':
      case 'turn.end':
      case 'turn.error':
        renderSessions();
        break;
      case 'error':
        toast({ tone: 'error', icon: 'alert', kicker: 'Server', title: event.title || 'Server error', text: event.message || '' });
        break;
      default:
        break;
    }
  }

  function startEvents() {
    var es = api.subscribeEvents(handleServerEvent, {
      onStatus: function (status) {
        state.events = status;
        renderStatus(state.status);
        IM.bus.emit('events.status', status);
      }
    });
    window.__imurun_eventsource = es;
    /* Coming back online or to the tab retries a stream that gave up. */
    window.addEventListener('online', function () { es.reconnect(); });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible' && state.events === 'offline') es.reconnect();
    });
    var statusRow = document.querySelector('.side-foot .status');
    if (statusRow) {
      statusRow.addEventListener('click', function () {
        if (state.events !== 'open') es.reconnect();
        IM.status.refresh();
      });
    }
  }

  /* ------------------------------------------------------------- start-up */

  async function loadInitialData() {
    var results = await Promise.allSettled([
      api.listConnectors(),
      api.listClients(),
      api.getUsage(),
      api.getResources(),
      api.listSessions(),
      api.listSkills()
    ]);
    if (results[0].status === 'fulfilled') renderConnectorGrid(results[0].value);
    else renderConnectorGrid([]);
    if (results[1].status === 'fulfilled') renderClients(results[1].value);
    if (results[2].status === 'fulfilled') updateUsageUI(results[2].value);
    if (results[3].status === 'fulfilled') renderResources(results[3].value);
    if (results[4].status === 'fulfilled') renderSessions(results[4].value);
    else {
      renderSessions([]);
      toast({ tone: 'error', icon: 'alert', kicker: 'Sessions', title: 'Could not load sessions', text: results[4].reason ? results[4].reason.message : 'The server did not answer.' });
    }
    if (results[5].status === 'fulfilled') renderSkills(results[5].value);
    else renderSkills([]);
    loadPersona();
    refreshStack();
    applyUrlParams();
  }

  function applyUrlParams() {
    var params;
    try { params = new URLSearchParams(location.search); } catch (error) { return; }
    var page = params.get('page');
    if (page && byId('page-' + page)) setPage(page);
    var settingsParam = params.get('settings');
    if (settingsParam) openSettings(settingsParam === '1' ? 'customisation' : settingsParam);
    if (params.get('rename') === '1') setTimeout(function () { openRename(document.querySelector('.session.active') || document.querySelector('.session')); }, 0);
    if (params.get('menu') === '1') {
      setTimeout(function () {
        var session = document.querySelector('.session');
        if (!session) return;
        var rect = session.getBoundingClientRect();
        showSessionMenu({ preventDefault: function () {}, clientX: rect.left + 22, clientY: rect.bottom + 6 }, session);
      }, 0);
    }
  }

  /* Re-render lists that show "replying" when a turn starts or settles. */
  IM.bus.on('turn.settled', function () { renderSessions(); });

  IM.shell = {
    setPage: setPage,
    currentPage: function () { return currentPage; },
    openSettings: openSettings,
    createSession: createSession,
    newChat: newChat,
    activateSession: activateSession,
    deleteSession: deleteSessionById,
    handleCommand: handleCommand,
    refreshHome: refreshHome,
    toggleSidebar: function () {
      if (isMobile()) setDrawer(!shell.classList.contains('nav-open'));
      else setSidebar(!shell.classList.contains('sidebar-collapsed'));
    },
    renameActive: function () { return handleCommand('/rename'); },
    /* Shared with connectors-settings.js (Settings > Connectors). */
    connectorIcons: CONNECTOR_ICONS,
    connectorCopy: CONNECTOR_COPY,
    refreshConnectorCard: function (id, connected, account) {
      var card = document.querySelector('#connector-grid [data-connector="' + String(id || '').replace(/"/g, '') + '"]');
      if (card) updateConnectorUI(card, connected, account);
    }
  };

  bindSessionLists();
  bindHome();
  setPage('home');
  if (IM.status) {
    IM.status.refresh();
    IM.status.startPolling();
  }
  startEvents();
  loadInitialData();
})();
