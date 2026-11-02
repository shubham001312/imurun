/* Voice, first step: browser speech-to-text for the composer and
 * text-to-speech for replies.
 *
 *   Mic button (#composer-mic)  SpeechRecognition / webkitSpeechRecognition
 *   "Read aloud" on replies     speechSynthesis
 *   Settings > Voice            language, voice, rate, sounds
 *
 * Settings live in the server `voice` section (settings.js):
 *   { lang: 'en-IN', voiceURI: '', rate: 1, sounds: true }
 * An empty lang means "follow the browser language".
 *
 * Privacy: Chrome's recognizer sends audio to Google's speech service, and
 * some engines do the same for online voices. The shell says so before the
 * first recording. A local STT/TTS engine on the server is planned; see
 * docs/frontend/VOICE.md and docs/frontend/API-REQUESTS.md.
 *
 * Missing APIs (Firefox has no recognition; WebView2 exposes the object but
 * the service refuses) degrade to a disabled button with the reason.
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var CONSENT_KEY = 'imurun.voice.consent.v1';
  var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  var synth = window.speechSynthesis || null;
  var hasTTS = Boolean(synth && window.SpeechSynthesisUtterance);

  var BASE_LANGS = [
    ['en-IN', 'English (India)'],
    ['en-US', 'English (United States)'],
    ['en-GB', 'English (United Kingdom)'],
    ['hi-IN', 'Hindi (India)'],
    ['bn-IN', 'Bengali (India)'],
    ['de-DE', 'German (Germany)']
  ];

  function toast(options) { return IM.toast ? IM.toast(options) : null; }
  function byId(id) { return document.getElementById(id); }

  function prefs() {
    var settings = window.__imurun_settings ? window.__imurun_settings.get() : null;
    var voice = (settings && settings.voice) || {};
    var rate = Number(voice.rate);
    return {
      lang: voice.lang || '',
      voiceURI: voice.voiceURI || '',
      rate: isFinite(rate) && rate >= 0.5 && rate <= 2 ? rate : 1,
      sounds: voice.sounds !== false
    };
  }

  function savePrefs(patch) {
    var settings = window.__imurun_settings;
    if (!settings) return;
    settings.set({ voice: patch });
    settings.save('voice');
  }

  function normaliseLang(lang) {
    return String(lang || '').replace('_', '-');
  }

  /* The language in use: the saved one, else the browser's if we know it,
   * else en-US. */
  function activeLang() {
    var saved = prefs().lang;
    if (saved) return saved;
    var browser = normaliseLang(navigator.language || 'en-US');
    var known = languages().map(function (pair) { return pair[0]; });
    if (known.indexOf(browser) !== -1) return browser;
    var prefix = browser.split('-')[0];
    var match = known.filter(function (code) { return code.split('-')[0] === prefix; })[0];
    return match || 'en-US';
  }

  function voices() {
    if (!hasTTS) return [];
    try { return synth.getVoices() || []; } catch (error) { return []; }
  }

  /* Base list plus every language the installed voices offer. */
  function languages() {
    var list = BASE_LANGS.slice();
    var seen = {};
    list.forEach(function (pair) { seen[pair[0]] = true; });
    var names = null;
    try { names = new Intl.DisplayNames([navigator.language || 'en'], { type: 'language' }); } catch (error) { names = null; }
    voices().forEach(function (voice) {
      var code = normaliseLang(voice.lang);
      if (!code || seen[code]) return;
      seen[code] = true;
      var label = code;
      if (names) {
        try { label = names.of(code) || code; } catch (error) { label = code; }
      }
      list.push([code, label]);
    });
    return list;
  }

  function voicesFor(lang) {
    var code = normaliseLang(lang).toLowerCase();
    var prefix = code.split('-')[0];
    var exact = voices().filter(function (voice) { return normaliseLang(voice.lang).toLowerCase() === code; });
    if (exact.length) return exact;
    return voices().filter(function (voice) { return normaliseLang(voice.lang).toLowerCase().split('-')[0] === prefix; });
  }

  /* A comfortable default: neural/natural voices first, then the platform
   * default, then local voices (they work offline). */
  function rankVoice(voice) {
    var name = voice.name || '';
    var score = 0;
    if (/natural|neural|online|premium|enhanced/i.test(name)) score += 4;
    if (/google/i.test(name)) score += 2;
    if (voice.default) score += 1;
    if (voice.localService) score += 0.5;
    if (/compact|espeak/i.test(name)) score -= 2;
    return score;
  }

  function pickVoice(lang) {
    var saved = prefs().voiceURI;
    var list = voicesFor(lang);
    if (saved) {
      var chosen = list.filter(function (voice) { return voice.voiceURI === saved; })[0];
      if (chosen) return chosen;
    }
    return list.slice().sort(function (a, b) { return rankVoice(b) - rankVoice(a); })[0] || null;
  }

  /* ------------------------------------------------------------ read aloud */

  var speaking = null; /* {button, cancelled} */

  /* Plain text for speech: drop code blocks, markup and link targets. */
  function speakable(markdown) {
    return String(markdown || '')
      .replace(/```[\s\S]*?(```|$)/g, ' (code block) ')
      .replace(/~~~[\s\S]*?(~~~|$)/g, ' (code block) ')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/https?:\/\/\S+/g, 'a link')
      .replace(/^\s{0,3}#{1,6}\s+/gm, '')
      .replace(/^\s*[-*+]\s+\[[ xX]\]\s+/gm, '')
      .replace(/^\s*([-*+]|\d+[.)])\s+/gm, '')
      .replace(/^\s*>\s?/gm, '')
      .replace(/\|/g, ', ')
      .replace(/^[\s,:-]+$/gm, '')
      .replace(/[*_~]{1,3}/g, '')
      .replace(/\n{2,}/g, '. ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  /* Long utterances stall in Chrome after ~15 s; speak sentence chunks. */
  function chunks(text) {
    var parts = text.match(/[^.!?।]+[.!?।]*\s*/g) || [text];
    var out = [];
    var buffer = '';
    parts.forEach(function (part) {
      if ((buffer + part).length > 220 && buffer) {
        out.push(buffer.trim());
        buffer = '';
      }
      buffer += part;
    });
    if (buffer.trim()) out.push(buffer.trim());
    return out;
  }

  function setSpeakButton(button, active) {
    if (!button) return;
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
    button.textContent = active ? 'Stop reading' : 'Read aloud';
    button.classList.toggle('is-active', active);
  }

  function stopSpeaking() {
    if (!speaking) return;
    speaking.cancelled = true;
    setSpeakButton(speaking.button, false);
    speaking = null;
    try { synth.cancel(); } catch (error) { /* ignore */ }
  }

  function speak(text, button) {
    if (!hasTTS) {
      toast({ tone: 'neutral', icon: 'info', kicker: 'Voice', title: 'Read aloud is not available', text: 'This browser or window has no speech synthesis.' });
      return;
    }
    var wasThis = speaking && speaking.button === button;
    stopSpeaking();
    if (wasThis) return;
    var plain = speakable(text);
    if (!plain) return;
    var lang = activeLang();
    var voice = pickVoice(lang);
    var rate = prefs().rate;
    var job = { button: button, cancelled: false };
    speaking = job;
    setSpeakButton(button, true);
    var queue = chunks(plain);
    function next() {
      if (job.cancelled) return;
      var piece = queue.shift();
      if (!piece) {
        if (speaking === job) speaking = null;
        setSpeakButton(button, false);
        return;
      }
      var utterance = new window.SpeechSynthesisUtterance(piece);
      utterance.lang = voice ? voice.lang : lang;
      if (voice) utterance.voice = voice;
      utterance.rate = rate;
      utterance.onend = next;
      utterance.onerror = function (event) {
        if (job.cancelled || (event && (event.error === 'interrupted' || event.error === 'canceled'))) return;
        if (speaking === job) speaking = null;
        setSpeakButton(button, false);
        toast({ tone: 'error', icon: 'alert', kicker: 'Voice', title: 'Could not read the reply aloud', text: (event && event.error) || 'The speech engine failed.' });
      };
      synth.speak(utterance);
    }
    next();
  }

  document.addEventListener('click', function (event) {
    var button = event.target && event.target.closest ? event.target.closest('[data-msg-speak]') : null;
    if (!button) return;
    var msg = button.closest('.msg');
    speak((msg && msg._answer) || '', button);
  });
  window.addEventListener('pagehide', stopSpeaking);

  /* ------------------------------------------------------- speech-to-text */

  var recognition = null;
  var listening = false;
  var micButton = null;
  var baseText = '';
  var serviceBroken = '';

  function micUnavailableReason() {
    if (!Recognition) return 'This browser has no speech recognition (Firefox and some embedded windows lack it).';
    if (!window.isSecureContext) return 'Speech recognition needs a secure page (https or localhost).';
    if (serviceBroken) return serviceBroken;
    return '';
  }

  function setMicState(on) {
    listening = on;
    if (!micButton) return;
    micButton.classList.toggle('is-listening', on);
    micButton.setAttribute('aria-pressed', on ? 'true' : 'false');
    micButton.setAttribute('aria-label', on ? 'Stop dictation' : 'Dictate a message');
    micButton.title = on ? 'Listening… click to stop' : 'Dictate (' + activeLang() + ')';
    document.body.classList.toggle('is-dictating', on);
  }

  function refreshMic() {
    if (!micButton) return;
    var reason = micUnavailableReason();
    micButton.disabled = Boolean(reason);
    micButton.classList.toggle('is-unavailable', Boolean(reason));
    if (reason) {
      micButton.title = reason;
      micButton.setAttribute('aria-label', 'Dictation unavailable: ' + reason);
    } else if (!listening) {
      setMicState(false);
    }
  }

  function prompt() { return document.querySelector('.input-wrap textarea'); }

  function startListening() {
    var reason = micUnavailableReason();
    if (reason) {
      toast({ tone: 'neutral', icon: 'info', kicker: 'Voice', title: 'Dictation is not available', text: reason });
      return;
    }
    if (!IM.store.get(CONSENT_KEY, false)) {
      var ok = window.confirm('Dictation uses your browser\'s speech recognizer. In Chrome and Edge your audio is sent to the browser vendor\'s speech service; it does not go through IMURUN. Continue?');
      if (!ok) return;
      IM.store.set(CONSENT_KEY, true);
    }
    var input = prompt();
    if (!input) return;
    try {
      recognition = new Recognition();
    } catch (error) {
      serviceBroken = 'Speech recognition could not start here.';
      refreshMic();
      return;
    }
    recognition.lang = activeLang();
    recognition.interimResults = true;
    recognition.continuous = true;
    recognition.maxAlternatives = 1;
    baseText = input.value ? input.value.replace(/\s*$/, ' ') : '';

    recognition.onresult = function (event) {
      var finalText = '';
      var interim = '';
      for (var i = 0; i < event.results.length; i += 1) {
        var result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else interim += result[0].transcript;
      }
      input.value = baseText + finalText + interim;
      input.dispatchEvent(new Event('input'));
    };
    recognition.onerror = function (event) {
      var code = event && event.error;
      var messages = {
        'not-allowed': 'Microphone access was refused. Allow the microphone for this page and try again.',
        'service-not-allowed': 'This window does not allow the speech service (common in embedded app windows).',
        'network': 'The browser\'s speech service could not be reached. It needs an internet connection.',
        'no-speech': 'No speech was heard.',
        'audio-capture': 'No microphone was found.',
        'language-not-supported': 'The recognizer does not support ' + activeLang() + '. Pick another language in Settings → Voice.'
      };
      if (code === 'aborted') return;
      if (code === 'service-not-allowed' || code === 'network') {
        serviceBroken = messages[code];
      }
      toast({ tone: code === 'no-speech' ? 'neutral' : 'error', icon: code === 'no-speech' ? 'info' : 'alert', kicker: 'Dictation',
        title: code === 'no-speech' ? 'Nothing heard' : 'Dictation stopped', text: messages[code] || ('Speech recognition error: ' + code) });
    };
    recognition.onend = function () {
      setMicState(false);
      recognition = null;
      refreshMic();
      var field = prompt();
      if (field) field.focus();
    };
    try {
      recognition.start();
      setMicState(true);
      util.announce('Listening');
    } catch (error) {
      setMicState(false);
      toast({ tone: 'error', icon: 'alert', kicker: 'Dictation', title: 'Could not start dictation', text: error.message });
    }
  }

  function stopListening() {
    if (recognition) {
      try { recognition.stop(); } catch (error) { /* ignore */ }
    }
    setMicState(false);
  }

  function bindMic() {
    micButton = byId('composer-mic');
    if (!micButton) return;
    micButton.addEventListener('click', function () {
      if (listening) stopListening();
      else startListening();
    });
    /* Sending a message ends dictation so the next words do not reappear. */
    document.addEventListener('keydown', function (event) {
      if (listening && event.key === 'Enter' && !event.shiftKey && event.target === prompt()) stopListening();
    }, true);
    refreshMic();
  }

  /* ------------------------------------------------------- settings panel */

  function fillLanguageSelect() {
    var select = byId('voice-lang');
    if (!select) return;
    var current = prefs().lang;
    var list = languages();
    select.innerHTML = '<option value="">Follow the browser (' + util.escapeHtml(activeLangFor('')) + ')</option>' +
      list.map(function (pair) {
        var count = voicesFor(pair[0]).length;
        return '<option value="' + util.escapeHtml(pair[0]) + '">' + util.escapeHtml(pair[1]) + ' — ' + pair[0] +
          (hasTTS ? (count ? '' : ' (no voice installed)') : '') + '</option>';
      }).join('');
    select.value = current;
  }

  function activeLangFor(saved) {
    if (saved) return saved;
    return normaliseLang(navigator.language || 'en-US');
  }

  function fillVoiceSelect() {
    var select = byId('voice-voice');
    if (!select) return;
    var lang = activeLang();
    var list = voicesFor(lang);
    var best = pickVoice(lang);
    if (!hasTTS) {
      select.innerHTML = '<option value="">Speech synthesis unavailable</option>';
      select.disabled = true;
      return;
    }
    if (!list.length) {
      select.innerHTML = '<option value="">No voice for ' + util.escapeHtml(lang) + ' on this system</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    select.innerHTML = '<option value="">Automatic' + (best ? ' (' + util.escapeHtml(best.name) + ')' : '') + '</option>' +
      list.map(function (voice) {
        return '<option value="' + util.escapeHtml(voice.voiceURI) + '">' + util.escapeHtml(voice.name) +
          (voice.localService ? '' : ' — online') + '</option>';
      }).join('');
    var saved = prefs().voiceURI;
    select.value = list.some(function (voice) { return voice.voiceURI === saved; }) ? saved : '';
  }

  function renderSupport() {
    var box = byId('voice-support');
    if (!box) return;
    var mic = micUnavailableReason();
    box.innerHTML =
      '<div class="settings-line"><span class="k">Dictation</span><span class="v">' +
        (mic ? util.escapeHtml(mic) : 'Available (browser recognizer)') + '</span>' +
        '<span class="chip ' + (mic ? 'neutral' : 'success') + '">' + (mic ? 'unavailable' : 'ready') + '</span></div>' +
      '<div class="settings-line"><span class="k">Read aloud</span><span class="v">' +
        (hasTTS ? voices().length + ' voices installed' : 'This browser or window has no speech synthesis') + '</span>' +
        '<span class="chip ' + (hasTTS ? 'success' : 'neutral') + '">' + (hasTTS ? 'ready' : 'unavailable') + '</span></div>';
  }

  function syncPanel() {
    var p = prefs();
    fillLanguageSelect();
    fillVoiceSelect();
    var rate = byId('voice-rate');
    if (rate && rate !== document.activeElement) rate.value = p.rate;
    var rateOut = byId('voice-rate-out');
    if (rateOut) rateOut.textContent = p.rate.toFixed(2) + '×';
    var sounds = byId('voice-sounds');
    /* One remembered sound toggle (sound.js, as on imutechz.in). */
    if (sounds) sounds.checked = IM.sound ? !IM.sound.isMuted() : p.sounds;
    renderSupport();
    refreshMic();
  }

  function bindPanel() {
    var lang = byId('voice-lang');
    if (lang) {
      lang.addEventListener('change', function () {
        savePrefs({ lang: lang.value, voiceURI: '' });
        syncPanel();
      });
    }
    var voice = byId('voice-voice');
    if (voice) voice.addEventListener('change', function () { savePrefs({ voiceURI: voice.value }); });
    var rate = byId('voice-rate');
    if (rate) {
      rate.addEventListener('input', function () {
        var out = byId('voice-rate-out');
        if (out) out.textContent = Number(rate.value).toFixed(2) + '×';
      });
      rate.addEventListener('change', function () { savePrefs({ rate: Number(rate.value) }); });
    }
    var sounds = byId('voice-sounds');
    if (sounds) sounds.addEventListener('change', function () { if (IM.sound) IM.sound.setMuted(!sounds.checked); savePrefs({ sounds: sounds.checked }); });
    var test = byId('voice-test');
    if (test) {
      test.addEventListener('click', function () {
        var samples = {
          hi: 'नमस्ते, मैं इमुरन हूँ। यह आवाज़ का एक नमूना है।',
          bn: 'নমস্কার, আমি ইমুরান। এটি একটি কণ্ঠস্বরের নমুনা।',
          de: 'Hallo, ich bin Imurun. Das ist eine Stimmprobe.'
        };
        var lang = activeLang().split('-')[0];
        speak(samples[lang] || 'Hello, I am Imurun. This is how replies will sound.', test);
      });
    }
    var chime = byId('voice-chime');
    if (chime) chime.addEventListener('click', function () { if (IM.sound) IM.sound.success(); });
  }

  function init() {
    bindMic();
    bindPanel();
    if (!hasTTS) document.body.classList.add('no-tts');
    syncPanel();
    if (hasTTS && 'onvoiceschanged' in synth) {
      synth.addEventListener('voiceschanged', syncPanel);
    }
    document.addEventListener('imurun:settings-hydrated', syncPanel);
  }

  IM.voice = {
    speak: speak,
    stop: stopSpeaking,
    startListening: startListening,
    stopListening: stopListening,
    activeLang: activeLang,
    speakable: speakable,
    supported: function () { return { stt: !micUnavailableReason(), tts: hasTTS }; }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
