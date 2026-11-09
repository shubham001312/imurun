/* Voice on the local server (services/voice, docs/voice/README.md,
 * docs/frontend/API-REQUESTS.md #33). voice.js keeps the browser path
 * (Web Speech API); this module takes over when the server can do it.
 *
 *   GET  /api/voice/status        what is available (and why not)
 *   POST /api/voice/transcribe    the recorded blob (MediaRecorder, WebM/Opus)
 *                                 -> {text, language, answer_language, ...};
 *                                 503 model_downloading + job (first use)
 *   POST /api/voice/speak         one sentence -> audio/wav; the next sentence
 *                                 is fetched while this one plays
 *   GET  /api/voice/voices        languages, voices, STT model sizes
 *   POST /api/voice/voices/install {engine, name} | {voice}  -> 202 {job}
 *   event voice.model.download    {id, kind, name, state, done, total, percent}
 *
 * Engine choice (`voice.engine`): auto = server when /api/voice/status says
 * so, else the browser; server; browser. The spoken language comes back as
 * `answer_language` and rides on the next turn as `language` (api.js).
 * The server never opens a microphone; the page records and posts the audio.
 * ES2017 (see docs/frontend/README.md).
 */
(function () {
  'use strict';

  var IM = window.IMURUN = window.IMURUN || {};
  var util = IM.util;
  var esc = util.escapeHtml;
  var ASKED_KEY = 'imurun.voice.languagesAsked.v1';
  var CORE = [['en', 'English'], ['hi', 'Hindi'], ['bn', 'Bengali'], ['de', 'German']];
  /* What a short phrase costs on this machine (docs/voice/README.md). */
  var MODEL_TIME = {
    base: 'A short phrase takes about 1.5-1.8 s.',
    small: 'A short phrase takes about 4-6 s.',
    'large-v3-turbo': 'A short phrase takes 23-42 s with this model.'
  };
  var MODEL_COST = {
    base: '148 MB · about 1.5-1.8 s per utterance here · fine for English and German, unusable for Hindi and Bengali',
    small: '486 MB · about 4.4-6.2 s per utterance on 2 threads · the default; Hindi works, Bengali is unreliable',
    'large-v3-turbo': '1.62 GB · 23-42 s per utterance on this CPU'
  };

  function api() { return window.__imurun_api; }
  function byId(id) { return document.getElementById(id); }
  function toast(options) { return IM.toast ? IM.toast(options) : null; }

  var state = { status: null, voices: null, checked: false, jobs: {}, recorder: null, chunks: [], stream: null,
    transcribing: false, pendingRetry: null, speaking: null, answerLanguage: '' };

  function vprefs() {
    var s = window.__imurun_settings ? window.__imurun_settings.get() : null;
    return Object.assign({}, (state.status && state.status.settings) || {}, (s && s.voice) || {});
  }

  function saveVoice(patch) {
    var s = window.__imurun_settings;
    if (!s) return Promise.resolve(false);
    s.set({ voice: patch });
    return s.save('voice');
  }

  /* --------------------------------------------------------------- engine */

  function engineChoice() { var e = vprefs().engine; return e === 'server' || e === 'browser' ? e : 'auto'; }
  function serverStt() { return engineChoice() !== 'browser' && Boolean(state.status && state.status.stt && state.status.stt.available); }
  function serverTts() { return engineChoice() !== 'browser' && Boolean(state.status && state.status.tts && state.status.tts.available); }

  function refreshStatus() {
    if (!api()) return Promise.resolve(null);
    return api().fetchAPI('/api/voice/status').then(function (s) {
      state.status = s || {};
      state.checked = true;
      syncMic();
      renderPanel();
      return s;
    }, function (error) {
      state.status = { stt: { available: false, reason: (error && error.body && (error.body.message || error.body.reason)) || (error && error.message) || 'The voice layer did not answer.' },
        tts: { available: false } };
      state.checked = true;
      syncMic();
      renderPanel();
      return null;
    });
  }

  /* -------------------------------------------------------------- the mic */

  function mic() { return byId('composer-mic'); }
  function prompt() { return document.querySelector('.input-wrap textarea'); }

  function syncMic() {
    var b = mic();
    if (!b) return;
    if (serverStt()) {
      b.disabled = false;
      b.classList.remove('is-unavailable');
      if (!state.recorder && !state.transcribing) {
        b.title = 'Dictate (on this computer)';
        b.setAttribute('aria-label', 'Dictate a message (speech is recognised on this computer)');
      }
    }
  }

  function setMic(mode) {
    var b = mic();
    if (!b) return;
    b.classList.toggle('is-listening', mode === 'recording');
    b.classList.toggle('is-transcribing', mode === 'transcribing');
    b.setAttribute('aria-pressed', mode === 'recording' ? 'true' : 'false');
    b.title = mode === 'recording' ? 'Recording… click to stop' : (mode === 'transcribing' ? 'Transcribing on this computer…' : 'Dictate (on this computer)');
    b.setAttribute('aria-label', mode === 'recording' ? 'Stop recording' : (mode === 'transcribing' ? 'Transcribing' : 'Dictate a message'));
    document.body.classList.toggle('is-dictating', mode === 'recording');
  }

  function pickMime() {
    var types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    for (var i = 0; i < types.length; i += 1) if (MediaRecorder.isTypeSupported(types[i])) return types[i];
    return '';
  }

  /* The model the server will use: the forced language's own model when one
   * is set (voice.sttModelByLanguage), else the default. With detection on
   * the default listens first; a language's model may then run a second pass. */
  function expectedModel() {
    var p = vprefs();
    var byLang = p.sttModelByLanguage || {};
    var forced = p.autoDetectLanguage === false && p.lang ? String(p.lang).split('-')[0] : '';
    return (forced && byLang[forced]) || p.sttModel || (state.status && state.status.stt && state.status.stt.model) || 'small';
  }

  function startRecording() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      toast({ tone: 'neutral', icon: 'info', kicker: 'Dictation', title: 'Recording is not available here', text: 'This window has no microphone recording (MediaRecorder).' });
      return;
    }
    var device = vprefs().inputDevice;
    navigator.mediaDevices.getUserMedia({ audio: device ? { deviceId: { exact: device } } : true }).then(function (stream) {
      var mime = pickMime();
      var rec;
      try { rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream); }
      catch (error) { stream.getTracks().forEach(function (t) { t.stop(); }); throw error; }
      state.stream = stream;
      state.chunks = [];
      state.recorder = rec;
      rec.addEventListener('dataavailable', function (e) { if (e.data && e.data.size) state.chunks.push(e.data); });
      rec.addEventListener('stop', function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        var type = rec.mimeType || mime || 'audio/webm';
        var blob = new Blob(state.chunks, { type: type });
        state.recorder = null;
        state.stream = null;
        transcribe(blob);
      });
      rec.start();
      setMic('recording');
      util.announce('Recording');
    }).catch(function (error) {
      var name = error && error.name;
      toast({ tone: 'error', icon: 'alert', kicker: 'Dictation', title: 'Could not record',
        text: name === 'NotAllowedError' ? 'Microphone access was refused. Allow the microphone for this window and try again.'
          : name === 'NotFoundError' ? 'No microphone was found.' : ((error && error.message) || 'The microphone could not be opened.') });
      setMic('idle');
    });
  }

  function stopRecording() {
    if (state.recorder && state.recorder.state !== 'inactive') state.recorder.stop();
  }

  /* -> POST the blob with its own Content-Type. */
  function postAudio(blob) {
    var p = vprefs();
    var lang = p.autoDetectLanguage === false && p.lang ? '?lang=' + encodeURIComponent(p.lang) : '';
    return api().baseUrl().then(function (base) {
      var headers = { 'Content-Type': blob.type || 'audio/webm' };
      var token = api().getToken();
      if (token) headers.Authorization = 'Bearer ' + token;
      return fetch(base + '/api/voice/transcribe' + lang, { method: 'POST', headers: headers, body: blob, credentials: 'omit', cache: 'no-store' });
    }).then(function (r) {
      return r.text().then(function (text) {
        var body = {};
        try { body = text ? JSON.parse(text) : {}; } catch (error) { body = {}; }
        return { status: r.status, ok: r.ok, body: body };
      });
    });
  }

  function transcribe(blob) {
    if (!blob || !blob.size) { setMic('idle'); return; }
    state.transcribing = true;
    setMic('transcribing');
    var expected = expectedModel();
    toast({ id: 'voice-stt', tone: 'neutral', icon: 'spinner', kicker: 'Dictation', title: 'Transcribing on this computer…',
      text: 'Model: ' + expected + '. ' + (MODEL_TIME[expected] || '') +
        (state.lastModel && state.lastModel !== expected ? ' Last phrase: ' + state.lastModel + '.' : ''), timeout: 0 });
    postAudio(blob).then(function (r) {
      state.transcribing = false;
      if (r.ok) {
        var b = r.body || {};
        if (b.model) {
          toast({ id: 'voice-stt', tone: 'success', icon: 'check', kicker: 'Dictation', title: 'Transcribed on this computer',
            text: 'Model: ' + b.model + (b.language_name ? ' · ' + b.language_name : ''), timeout: 2500 });
        } else if (IM.toast) {
          IM.toast.dismiss('voice-stt');
        }
        var text = String(b.text || '').trim();
        var input = prompt();
        if (!text) {
          toast({ tone: 'neutral', icon: 'info', kicker: 'Dictation', title: 'Nothing heard', text: 'No speech was recognised in that recording.' });
        } else if (input) {
          input.value = (input.value ? input.value.replace(/\s*$/, ' ') : '') + text;
          input.dispatchEvent(new Event('input'));
          input.focus();
        }
        var spoken = b.answer_language || b.language || '';
        state.answerLanguage = vprefs().replyInSameLanguage === false ? '' : spoken;
        /* which Whisper model produced the text (voice.sttModelByLanguage) */
        if (b.model) {
          state.lastModel = b.model;
          var m = mic();
          if (m) m.dataset.lastModel = b.model;
        }
        if (b.model_pending && b.model_pending.model) {
          var pend = b.model_pending;
          toast({ id: 'voice-model-pending', tone: pend.error ? 'error' : 'neutral', icon: pend.error ? 'alert' : 'spinner', kicker: 'Dictation',
            title: pend.error ? pend.model + ' did not download' : 'Downloading ' + pend.model + '…',
            text: (pend.error || pend.message || ('The ' + (b.language_name || spoken) + ' model is still downloading.')) +
              (b.model ? ' This phrase was transcribed with ' + b.model + '.' : ''), timeout: pend.error ? undefined : 8000 });
        }
        if (spoken && b.language_name) util.announce('Heard ' + b.language_name);
        setMic('idle');
        return;
      }
      var body = r.body || {};
      if (r.status === 503 && (body.error === 'model_downloading' || body.reason === 'model_downloading') && body.job) {
        /* first use: the model downloads, then this recording is sent again */
        state.pendingRetry = { blob: blob, jobId: body.job.id };
        noteJob(body.job);
        toast({ id: 'voice-stt', tone: 'neutral', icon: 'spinner', kicker: 'Dictation', title: 'Downloading the speech model first',
          text: jobText(body.job) + '. Your recording is kept and transcribed when it is done.', timeout: 0 });
        setMic('idle');
        return;
      }
      if (IM.toast) IM.toast.dismiss('voice-stt');
      toast({ tone: 'error', icon: 'alert', kicker: 'Dictation', title: 'Not transcribed', text: body.message || body.reason || body.error || ('HTTP ' + r.status) });
      setMic('idle');
    }, function (error) {
      state.transcribing = false;
      if (IM.toast) IM.toast.dismiss('voice-stt');
      toast({ tone: 'error', icon: 'alert', kicker: 'Dictation', title: 'Not transcribed', text: (error && error.message) || 'The server did not answer.' });
      setMic('idle');
    });
  }

  /* The mic click: the server path when it is available, else voice.js. */
  function onMicClick(event) {
    if (!serverStt()) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    if (state.transcribing) return;
    if (state.recorder) stopRecording();
    else startRecording();
  }

  /* -------------------------------------------------------- downloads */

  function jobText(job) {
    var pct = Math.round(Number(job.percent) || 0);
    var size = Number(job.total) > 0 ? ' of ' + Math.round(Number(job.total) / 1048576) + ' MB' : '';
    return (job.kind === 'whisper' ? 'Speech model ' : 'Voice ') + (job.name || '') + ': ' +
      (job.state === 'done' ? 'downloaded' : job.state === 'error' ? 'failed' : pct + ' %' + size);
  }

  function noteJob(job) {
    if (!job || !job.id) return;
    state.jobs[job.id] = job;
    renderJobs();
  }

  function onDownload(job) {
    if (!job || !job.id) return;
    noteJob(job);
    var retry = state.pendingRetry;
    var mine = retry && retry.jobId === job.id;
    if (mine) {
      toast({ id: 'voice-stt', tone: job.state === 'error' ? 'error' : 'neutral', icon: job.state === 'error' ? 'alert' : 'spinner', kicker: 'Dictation',
        title: job.state === 'error' ? 'The speech model did not download' : 'Downloading the speech model first',
        text: jobText(job) + (job.error ? ' · ' + job.error : ''), timeout: job.state === 'error' ? undefined : 0 });
    }
    if (job.state === 'done') {
      if (mine) { state.pendingRetry = null; transcribe(retry.blob); }
      state.voices = null;
      if (byId('voice-server')) loadVoices();
    }
  }

  /* ------------------------------------------------------------ speaking */

  /* Markdown -> sentences, the same rules as services/voice/text.py
   * (. ! ? । ॥ 。, at most 220 characters). */
  function sentences(markdown) {
    var plain = IM.voice && IM.voice.speakable ? IM.voice.speakable(markdown) : String(markdown || '');
    var parts = plain.replace(/\s+/g, ' ').match(/[^.!?।॥。]+[.!?।॥。]*\s*/g) || [];
    var out = [];
    parts.forEach(function (p) {
      var s = p.trim();
      while (s.length > 220) {
        var cut = s.lastIndexOf(' ', 220);
        if (cut < 80) cut = 220;
        out.push(s.slice(0, cut).trim());
        s = s.slice(cut).trim();
      }
      if (s) out.push(s);
    });
    return out;
  }

  function fetchSentence(text, lang) {
    var p = vprefs();
    var voice = (p.voices && lang && p.voices[lang]) || '';
    return api().baseUrl().then(function (base) {
      var headers = { 'Content-Type': 'application/json' };
      var token = api().getToken();
      if (token) headers.Authorization = 'Bearer ' + token;
      return fetch(base + '/api/voice/speak', { method: 'POST', headers: headers, credentials: 'omit', cache: 'no-store',
        body: JSON.stringify({ text: text, lang: lang || '', voice_id: voice, rate: Number(p.rate) || 1, raw: true }) });
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          var body = {};
          try { body = JSON.parse(t); } catch (error) { body = {}; }
          var e = new Error(body.message || body.reason || body.error || ('HTTP ' + r.status));
          e.status = r.status; e.body = body;
          throw e;
        });
      }
      return r.blob().then(function (blob) {
        return { blob: blob, engine: r.headers.get('X-Voice-Engine') || '', voice: r.headers.get('X-Voice-Id') || '',
          offline: r.headers.get('X-Voice-Offline') !== 'false', fallback: r.headers.get('X-Voice-Fallback') || '' };
      });
    });
  }

  function setSpeakButton(button, on) {
    if (!button) return;
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    button.textContent = on ? 'Stop reading' : 'Read aloud';
    button.classList.toggle('is-active', on);
  }

  function stopSpeaking() {
    var job = state.speaking;
    if (!job) return;
    job.cancelled = true;
    if (job.audio) { try { job.audio.pause(); } catch (error) { /* ignore */ } }
    if (job.url) URL.revokeObjectURL(job.url);
    setSpeakButton(job.button, false);
    state.speaking = null;
    document.body.classList.remove('is-speaking');
  }

  /* Sentence n plays while n+1 is fetched (Piper is ~20x real time). */
  function speak(markdown, button, lang) {
    var wasThis = state.speaking && state.speaking.button === button;
    stopSpeaking();
    if (wasThis && button) return;
    var list = sentences(markdown);
    if (!list.length) return;
    var language = lang || state.lastLanguage || '';
    var job = { button: button, cancelled: false, audio: null, url: '', index: 0 };
    state.speaking = job;
    setSpeakButton(button, true);
    document.body.classList.add('is-speaking');
    var next = fetchSentence(list[0], language);
    function step() {
      if (job.cancelled) return;
      var current = next;
      var i = job.index;
      next = i + 1 < list.length ? fetchSentence(list[i + 1], language) : null;
      if (next) next.catch(function () {});
      current.then(function (piece) {
        if (job.cancelled) return;
        if (button) button.title = 'Voice: ' + (piece.voice || piece.engine) + (piece.offline ? ' (on this computer)' : ' (online: the text went to Microsoft)');
        if (job.url) URL.revokeObjectURL(job.url);
        job.url = URL.createObjectURL(piece.blob);
        var audio = new Audio(job.url);
        job.audio = audio;
        audio.addEventListener('ended', function () {
          job.index += 1;
          if (job.index >= list.length) { if (state.speaking === job) stopSpeaking(); return; }
          step();
        });
        audio.addEventListener('error', function () { if (state.speaking === job) stopSpeaking(); });
        var played = audio.play();
        if (played && played.catch) played.catch(function () { if (state.speaking === job) stopSpeaking(); });
      }, function (error) {
        if (job.cancelled) return;
        if (state.speaking === job) stopSpeaking();
        var b = (error && error.body) || {};
        if (error && error.status === 503 && b.job) {
          noteJob(b.job);
          toast({ tone: 'neutral', icon: 'spinner', kicker: 'Read aloud', title: 'Downloading a voice first', text: jobText(b.job) + '. Try again when it is done.' });
          return;
        }
        toast({ tone: 'error', icon: 'alert', kicker: 'Read aloud', title: 'Could not read the reply aloud', text: (error && error.message) || 'The voice layer failed.' });
      });
    }
    step();
  }

  function onSpeakClick(event) {
    var sample = event.target && event.target.closest ? event.target.closest('#voice-test') : null;
    if (sample && serverTts()) {
      event.stopImmediatePropagation();
      event.preventDefault();
      var lang = (vprefs().languages || ['en'])[0] || 'en';
      var samples = { en: 'Hello, I am Imurun. This is how replies will sound.', de: 'Hallo, ich bin Imurun. So klingen die Antworten.',
        hi: 'नमस्ते, मैं इमुरन हूँ।',
        bn: 'নমস্কার, আমি ইমুরান।' };
      speak(samples[lang] || samples.en, null, lang);
      return;
    }
    var button = event.target && event.target.closest ? event.target.closest('[data-msg-speak]') : null;
    if (!button || !serverTts()) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    var msg = button.closest('.msg');
    speak((msg && msg._answer) || '', button);
  }

  /* `speakReplies`: read each new reply when it finishes. */
  function onTurnStats(stats) {
    if (!serverTts() || !vprefs().speakReplies || !stats || stats.status === 'error') return;
    var node = stats.turnId ? document.querySelector('.msg.assistant[data-turn-id="' + String(stats.turnId).replace(/"/g, '') + '"]') : null;
    if (!node || !node._answer) return;
    speak(node._answer, node.querySelector('[data-msg-speak]'), state.lastLanguage);
  }

  /* ---------------------------------------------------- settings panel */

  function asked() { try { return window.localStorage.getItem(ASKED_KEY) === '1'; } catch (error) { return false; } }

  function loadVoices() {
    if (!api()) return;
    api().fetchAPI('/api/voice/voices').then(function (v) { state.voices = v || {}; renderPanel(); }, function () { state.voices = { error: true }; renderPanel(); });
  }

  function langName(code) {
    var l = state.voices && state.voices.languages && state.voices.languages[code];
    if (l && l.name) return l.name + (l.native && l.native !== l.name ? ' (' + l.native + ')' : '');
    var core = CORE.filter(function (c) { return c[0] === code; })[0];
    return core ? core[1] : code;
  }

  function modelSize(name) {
    var list = (state.voices && state.voices.stt_models) || [];
    var m = list.filter(function (x) { return x.name === name; })[0];
    return m ? m : null;
  }

  function renderJobs() {
    var box = byId('voice-jobs');
    if (!box) return;
    var jobs = Object.keys(state.jobs).map(function (k) { return state.jobs[k]; }).filter(function (j) { return j.state !== 'done'; });
    box.hidden = !jobs.length;
    box.innerHTML = jobs.map(function (j) {
      var pct = Math.max(0, Math.min(100, Number(j.percent) || 0));
      return '<div class="voice-job"><span>' + esc(jobText(j)) + '</span><div class="tune-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + Math.round(pct) +
        '" aria-label="' + esc((j.name || '') + ' download') + '"><span style="width:' + pct + '%"></span></div>' + (j.error ? '<p class="tune-error">' + esc(j.error) + '</p>' : '') + '</div>';
    }).join('');
  }

  function renderPanel() {
    var box = byId('voice-server');
    if (!box) return;
    /* never rebuild under a field the user is typing in */
    var active = document.activeElement;
    if (box.dataset.built === '1' && active && box.contains(active) && active.matches('input[type="number"], input[type="text"], textarea')) { renderJobs(); return; }
    var s = state.status || {};
    var p = vprefs();
    var stt = s.stt || {};
    var tts = s.tts || {};
    var langs = Array.isArray(p.languages) && p.languages.length ? p.languages : ['en', 'hi', 'bn', 'de'];
    var model = p.sttModel || stt.model || 'small';
    var byLang = p.sttModelByLanguage || {};
    var html = '<h3 class="settings-card-title">Speech on this computer</h3>';
    if (!state.checked) {
      box.innerHTML = html + '<p class="settings-note">Checking the voice layer…</p>';
      return;
    }
    html += '<dl class="tune-facts">' +
      '<dt>Dictation</dt><dd>' + (stt.available ? 'on this computer (' + esc(stt.engine || 'faster-whisper') + (stt.model_installed === false ? ', model downloads on first use' : '') + ')' : 'not available: ' + esc(stt.reason || 'unknown')) + '</dd>' +
      '<dt>Read aloud</dt><dd>' + (tts.available ? 'on this computer (' + esc(tts.engine || 'piper') + ')' : 'not available: ' + esc(tts.reason || ((tts.engines && tts.engines.piper && tts.engines.piper.reason) || 'unknown'))) + '</dd>' +
      '</dl>' +
      '<label class="form-field"><span class="k">Use</span><select data-voice-set="engine">' +
        [['auto', 'Automatic: this computer when available, else the browser'], ['server', 'This computer only'], ['browser', 'The browser\'s speech service']].map(function (o) {
          return '<option value="' + o[0] + '"' + (engineChoice() === o[0] ? ' selected' : '') + '>' + esc(o[1]) + '</option>'; }).join('') + '</select></label>';

    /* first run: which languages do you speak? */
    html += '<details class="voice-langs" data-voice-langs' + (asked() ? '' : ' open') + '><summary>' + (asked() ? 'Languages you speak: ' + esc(langs.map(langName).join(', ')) : 'Which languages do you speak?') + '</summary>' +
      '<p class="settings-note">Recognition is limited to these, and the speech model is chosen from them: English and German only need the small, fast <code>base</code> model; Hindi or Bengali need <code>small</code>.</p>' +
      '<div class="voice-lang-list">' + CORE.map(function (c) {
        return '<label class="voice-lang"><input type="checkbox" data-voice-lang="' + c[0] + '"' + (langs.indexOf(c[0]) !== -1 ? ' checked' : '') + '> ' + esc(langName(c[0])) + '</label>';
      }).join('') + '</div>' +
      '<label class="toggle-row voice-turbo"><span><strong>Bengali: use the large-v3-turbo model</strong><small>More accurate for Bengali (character error ~0.27 instead of ~0.35 on synthetic speech) but 1.62 GB and 23-42 s per utterance on this CPU. ' +
        'Used for Bengali only: with a detected language the default model listens first, then this one transcribes again.</small></span><input type="checkbox" data-voice-turbo' + (byLang.bn === 'large-v3-turbo' ? ' checked' : '') + '></label>' +
      '<button class="btn primary" type="button" data-voice-savelangs>Save languages</button></details>';

    /* model */
    var sizes = ['base', 'small', 'large-v3-turbo'];
    html += '<label class="form-field"><span class="k">Speech model</span><select data-voice-set="sttModel">' + sizes.map(function (m) {
      var info = modelSize(m);
      return '<option value="' + m + '"' + (model === m ? ' selected' : '') + '>' + m + (info ? ' · ' + info.download_mb + ' MB' + (info.installed ? ' · installed' : '') : '') + '</option>';
    }).join('') + '</select><small class="tool-field-hint">' + esc(MODEL_COST[model] || '') + '</small></label>';

    /* per-language voices */
    var vl = (state.voices && state.voices.languages) || {};
    html += '<div class="voice-voices"><div class="settings-label">Voices</div>' + (state.voices && !state.voices.error ? langs.map(function (code) {
      var entry = vl[code] || {};
      var list = Array.isArray(entry.voices) ? entry.voices : [];
      var current = (p.voices && p.voices[code]) || entry.selected || '';
      if (!list.length) return '<p class="settings-note">' + esc(langName(code)) + ': no voice listed.</p>';
      var opts = list.map(function (v) {
        var id = (v.engine || '') + ':' + (v.voice || '');
        var label = (v.name || v.voice) + ' · ' + (v.engine || '') + (v.quality ? ' · ' + v.quality : '') +
          (v.installed ? ' · installed' : (v.download_mb ? ' · ' + v.download_mb + ' MB download' : '')) + (v.offline === false ? ' · ONLINE (sends text to Microsoft)' : '');
        return '<option value="' + esc(id) + '"' + (current === id ? ' selected' : '') + '>' + esc(label) + '</option>';
      }).join('');
      var sel = list.filter(function (v) { return (v.engine + ':' + v.voice) === current; })[0] || list.filter(function (v) { return v.default; })[0] || list[0];
      var needsInstall = sel && sel.engine === 'piper' && !sel.installed;
      return '<div class="voice-voice-row"><label class="form-field"><span class="k">' + esc(langName(code)) + '</span><select data-voice-voice="' + esc(code) + '"><option value="">Automatic</option>' + opts + '</select></label>' +
        (needsInstall ? '<button class="btn" type="button" data-voice-install="piper:' + esc(sel.voice) + '">Install' + (sel.download_mb ? ' (' + sel.download_mb + ' MB)' : '') + '</button>' : '') + '</div>';
    }).join('') : '<p class="settings-note">' + (state.voices && state.voices.error ? 'The voice list could not be loaded.' : 'Loading the voice list…') + '</p>') + '</div>';

    var threads = Number(p.threads) || 2;
    html += '<label class="toggle-row"><span><strong>Read replies aloud</strong><small>Every new reply, sentence by sentence, on this computer.</small></span><input type="checkbox" data-voice-set="speakReplies"' + (p.speakReplies ? ' checked' : '') + '></label>' +
      '<label class="toggle-row"><span><strong>Detect the spoken language</strong><small>Off: always recognise the language chosen above under "Language".</small></span><input type="checkbox" data-voice-set="autoDetectLanguage"' + (p.autoDetectLanguage !== false ? ' checked' : '') + '></label>' +
      '<label class="toggle-row"><span><strong>Answer in the language I spoke</strong></span><input type="checkbox" data-voice-set="replyInSameLanguage"' + (p.replyInSameLanguage !== false ? ' checked' : '') + '></label>' +
      '<label class="form-field"><span class="k">Threads for speech</span><input type="number" min="1" max="16" data-voice-set="threads" value="' + threads + '">' +
        '<small class="tool-field-hint">2 by default. 4 is about 30 % faster here (3.2-4.1 s instead of 4.4-6.2 s per utterance) but takes CPU from the model while it answers.</small></label>' +
      '<label class="toggle-row"><span><strong>Online voices (sends text to Microsoft)</strong><small>Off by default. When on, a language with no voice on this computer is spoken by Microsoft\'s online service: the reply text leaves this machine.</small></span>' +
        '<input type="checkbox" data-voice-set="onlineFallback"' + (p.onlineFallback === true ? ' checked' : '') + '></label>' +
      '<div id="voice-jobs" class="voice-jobs" hidden></div>' +
      '<p class="settings-note voice-accuracy"><strong>Measured accuracy</strong> (synthetic speech: Piper reading a sentence, Whisper transcribing it; real microphone speech has not been measured yet): ' +
        'English and German about 1 word in 10 differs (mostly "twelve" written as "12"); Hindi works (character error 0.08-0.12). ' +
        '<strong>Bengali is usable but unreliable: it fails roughly 1 run in 6</strong>, and the other runs still get about a third of the characters wrong.</p>';
    box.innerHTML = html;
    box.dataset.built = '1';
    renderJobs();
  }

  function saveLanguages(box) {
    var langs = Array.prototype.filter.call(box.querySelectorAll('[data-voice-lang]'), function (c) { return c.checked; })
      .map(function (c) { return c.getAttribute('data-voice-lang'); });
    if (!langs.length) { toast({ tone: 'neutral', icon: 'info', kicker: 'Voice', title: 'Pick at least one language', text: '' }); return; }
    var onlyFast = langs.every(function (l) { return l === 'en' || l === 'de'; });
    var turbo = box.querySelector('[data-voice-turbo]');
    var byLang = Object.assign({}, vprefs().sttModelByLanguage || {});
    if (turbo && turbo.checked && langs.indexOf('bn') !== -1) byLang.bn = 'large-v3-turbo'; else delete byLang.bn;
    var patch = { languages: langs, sttModel: onlyFast ? 'base' : 'small', sttModelByLanguage: byLang };
    try { window.localStorage.setItem(ASKED_KEY, '1'); } catch (error) { /* ignore */ }
    saveVoice(patch).then(function () {
      toast({ tone: 'success', icon: 'check', kicker: 'Voice', title: 'Languages saved',
        text: 'Speech model: ' + patch.sttModel + (onlyFast ? ' (fast, enough for English and German)' : ' (needed for Hindi and Bengali)') + '.' });
      refreshStatus();
      loadVoices();
    });
  }

  function install(id, button) {
    var parts = String(id).split(':');
    button.disabled = true;
    button.textContent = 'Starting…';
    api().fetchAPI('/api/voice/voices/install', { method: 'POST', body: JSON.stringify(parts[0] === 'piper' ? { voice: id } : { engine: parts[0], name: parts[1] }) })
      .then(function (r) { if (r && r.job) noteJob(r.job); button.textContent = 'Downloading…'; },
        function (error) { button.disabled = false; button.textContent = 'Install'; toast({ tone: 'error', icon: 'alert', kicker: 'Voice', title: 'Could not start the download', text: error.message }); });
  }

  function bindPanel(box) {
    if (box.dataset.bound === '1') return;
    box.dataset.bound = '1';
    box.addEventListener('change', function (event) {
      var t = event.target;
      var key = t.getAttribute && t.getAttribute('data-voice-set');
      if (key) {
        var value = t.type === 'checkbox' ? t.checked : (t.type === 'number' ? Math.max(1, Math.min(16, Number(t.value) || 2)) : t.value);
        var patch = {};
        patch[key] = value;
        saveVoice(patch).then(function () { if (key === 'engine' || key === 'sttModel') refreshStatus(); else renderPanel(); });
        return;
      }
      var lang = t.getAttribute && t.getAttribute('data-voice-voice');
      if (lang !== null && lang !== undefined && t.matches('[data-voice-voice]')) {
        var voices = Object.assign({}, vprefs().voices || {});
        voices[lang] = t.value;
        saveVoice({ voices: voices }).then(renderPanel);
      }
    });
    box.addEventListener('click', function (event) {
      var b = event.target.closest('button');
      if (!b) return;
      if (b.hasAttribute('data-voice-savelangs')) saveLanguages(box);
      else if (b.hasAttribute('data-voice-install')) install(b.getAttribute('data-voice-install'), b);
    });
  }

  function onSection(section) {
    if (section !== 'voice') return;
    var box = byId('voice-server');
    if (!box) return;
    bindPanel(box);
    refreshStatus();
    if (!state.voices) loadVoices();
  }

  /* ---------------------------------------------------------------- init */

  function init() {
    /* capture phase: decide before voice.js's own handlers run */
    document.addEventListener('click', function (event) {
      var m = event.target && event.target.closest ? event.target.closest('#composer-mic') : null;
      if (m) onMicClick(event);
      else onSpeakClick(event);
    }, true);
    if (IM.bus) {
      IM.bus.on('settings.section', onSection);
      IM.bus.on('server:voice.model.download', onDownload);
      IM.bus.on('turn.stats', onTurnStats);
    }
    document.addEventListener('imurun:settings-hydrated', function () { syncMic(); renderPanel(); });
    window.addEventListener('pagehide', stopSpeaking);
    refreshStatus();
  }

  IM.voiceServer = {
    refresh: refreshStatus,
    speak: speak,
    stop: stopSpeaking,
    sentences: sentences,
    available: function () { return { stt: serverStt(), tts: serverTts() }; },
    /* api.js streamTurn: the language the last dictation was in, used once */
    takeAnswerLanguage: function () {
      var lang = state.answerLanguage;
      state.answerLanguage = '';
      if (lang) state.lastLanguage = lang;
      return lang;
    },
    _state: state
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
