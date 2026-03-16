"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   See & Tell — app.js  (clean rewrite)

   SAPI ECHO PROBLEM & SOLUTION:
   Windows SAPI speaks through the OS. The browser never knows when it
   finishes. So instead of tracking TTS-end time, we estimate how long
   each SAPI response takes to speak (~130 words/min) and mute the mic
   for exactly that long after each command dispatch.

   SHORT responses ("Captions stopped." ~1s) → 2s mute
   MEDIUM responses ("Search mode on. What are you looking for?" ~3s) → 4s mute
   LONG responses ("Your phone was last seen..." ~4s) → 5s mute
   ═══════════════════════════════════════════════════════════════════════════ */

function log(cat, msg) {
    console.log(`[${new Date().toTimeString().slice(0,8)}] [${cat}] ${msg}`);
}

/* ─── State ──────────────────────────────────────────────────────────────── */
window.USER_SETTINGS      = window.USER_SETTINGS || { auto_speak:true, speech_rate:160 };
window.VOICE_MODE         = false;

let isPolling        = false;
let pollTimer        = null;
let errorCount       = 0;
let cmdInFlight      = false;
let lastCmdText      = "";
let lastCmdTime      = 0;
let micUnblockedAt   = 0;   // mic is blocked until this timestamp

const DEDUP_MS   = 1500;
const POLL_MS    = 1000;   // 1s — catches new captions within 1s of BLIP firing
const BG_POLL_MS = 5000;   // 5s when tab is in background

// Estimate browser TTS speaking time so mic isn't unblocked while speech plays.
// Browser TTS at rate≈1.1 (our default) runs ~175 wpm — much faster than SAPI.
function _sapiMuteMs(text, isSearchResult) {
    const words  = (text || "").split(/\s+/).length;
    const sr     = (window.USER_SETTINGS && window.USER_SETTINGS.speech_rate) || 165;
    const wpm    = 130 + ((sr - 140) / 50) * 90;   // 140→130, 165→175, 190→220 wpm
    const speakMs = (words / wpm) * 60 * 1000;
    // Search results: add thinking time on top of speaking time
    const buffer = isSearchResult ? 2500 : 1200;
    return Math.max(1500, speakMs + buffer);
}

function _blockMicFor(ms) {
    micUnblockedAt = Date.now() + ms;
    log("MIC", `Blocked for ${ms}ms (until +${ms}ms)`);
}

function _micIsBlocked() {
    return Date.now() < micUnblockedAt;
}

/* ═══════════════════════════════════════════════════════════════════════════
   CAPTION POLLING
   ═══════════════════════════════════════════════════════════════════════════ */
function startPolling() {
    if (isPolling) return;
    isPolling = true; errorCount = 0;
    log("POLL","Started");
    _doPoll();
}
function stopPolling() {
    isPolling = false;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}
function _doPoll() {
    fetchCaption().finally(() => {
        if (isPolling)
            pollTimer = setTimeout(_doPoll, document.hidden ? BG_POLL_MS : POLL_MS);
    });
}
document.addEventListener("visibilitychange", () => {
    if (isPolling) { stopPolling(); startPolling(); }
});
async function fetchCaption() {
    try {
        const r = await fetch("/api/caption", { credentials:"same-origin" });
        if (!r.ok) throw new Error("HTTP "+r.status);
        const d = await r.json();
        errorCount = 0;

        // Always update caption text directly — never rely on updateUI timing.
        // On mobile, updateUI may not be defined yet on first poll (script load race).
        const captionEl = document.getElementById("caption-text");
        if (captionEl && d.caption) captionEl.textContent = d.caption;

        // Speak new captions via browser TTS (speak_text is non-null only for new events)
        if (d.speak_text && d.speak_text !== window._lastSpokenCaption) {
            window._lastSpokenCaption = d.speak_text;
            const autoSpeak = !window.USER_SETTINGS || window.USER_SETTINGS.auto_speak !== false;
            if (autoSpeak) {
                log("TTS", `Speaking: "${d.speak_text.slice(0,50)}"`);
                speakText(d.speak_text, 0);
            }
        }
        if (typeof window.updateUI === "function") window.updateUI(d);
    } catch(e) {
        if (++errorCount >= 5) {
            stopPolling();
            const el = document.getElementById("caption-text");
            if (el) el.textContent = "Connection lost. Please refresh.";
        }
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
   TTS — browser Web Speech API (works on desktop + mobile over Wi-Fi)
   ═══════════════════════════════════════════════════════════════════════════ */

// iOS Safari requires a user gesture to unlock audio context.
// We fire a silent utterance on first tap so all subsequent calls work.
let _ttsUnlocked = false;
function _unlockTTS() {
    if (_ttsUnlocked || !window.speechSynthesis) return;
    // Chrome requires actual text (not empty string) to unlock the audio context
    const u = new SpeechSynthesisUtterance('.');
    u.volume = 0;
    u.rate   = 10;   // speak at max rate so the dot is inaudible
    window.speechSynthesis.speak(u);
    _ttsUnlocked = true;
    log("TTS", "Audio context unlocked");
}
document.addEventListener('click',     _unlockTTS, { once: true });
document.addEventListener('touchstart', _unlockTTS, { once: true });

// Chrome Android bug: speechSynthesis silently pauses after ~15s.
// Fix: pause+resume it every 10s while it is actively speaking.
let _ttsTimer = null;
function _ttsKeepAlive() {
    _clearTTSTimer();
    _ttsTimer = setInterval(() => {
        if (window.speechSynthesis && window.speechSynthesis.speaking) {
            window.speechSynthesis.pause();
            window.speechSynthesis.resume();
        } else { _clearTTSTimer(); }
    }, 10000);
}
function _clearTTSTimer() {
    if (_ttsTimer) { clearInterval(_ttsTimer); _ttsTimer = null; }
}

function speakText(text, delayMs) {
    if (!text || !text.trim()) return;
    if (!window.speechSynthesis) return;
    function _fire() {
        window.speechSynthesis.cancel();
        // 50ms gap lets mobile TTS engine reset after cancel()
        setTimeout(() => {
            const u = new SpeechSynthesisUtterance(text);
            const sr = (window.USER_SETTINGS && window.USER_SETTINGS.speech_rate) || 165;
            u.rate   = 0.85 + ((sr - 140) / 50) * 0.55;
            u.pitch  = 1.0;
            u.volume = 1.0;
            u.lang   = "en-US";
            u.onstart = _ttsKeepAlive;
            u.onend   = _clearTTSTimer;
            u.onerror = _clearTTSTimer;
            window.speechSynthesis.speak(u);
        }, 50);
    }
    // Skip outer setTimeout when delay is 0 — fires immediately
    if (!delayMs || delayMs <= 0) _fire();
    else setTimeout(_fire, delayMs);
}

function readAloud() {
    const el = document.getElementById("caption-text");
    const t  = el ? el.textContent : "";
    if (t && t.length > 3) speakText(t, 0);
}

/* ═══════════════════════════════════════════════════════════════════════════
   MEMORY
   ═══════════════════════════════════════════════════════════════════════════ */
function addToMemory() { sendCmd("save"); }
function clearMemory() {
    if (!confirm("Clear all saved memory?")) return;
    fetch("/api/memory/clear",{method:"POST",credentials:"same-origin"})
        .then(()=>{ loadMemory(); });
}
function loadMemory() {
    fetch("/api/memory",{credentials:"same-origin"})
        .then(r=>r.json())
        .then(d=>{
            const tb = document.getElementById("memory-table-body");
            if (!tb) return;
            const items = d.memories||[];
            if (!items.length) {
                tb.innerHTML = '<tr><td colspan="4" style="color:#555;font-style:italic">No memories saved yet.</td></tr>';
                const cnt = document.getElementById("entry-count");
                if (cnt) cnt.textContent = "";
                return;
            }
            // Count sightings per object
            const counts = {};
            items.forEach(m => {
                const k = (m.object||"").toLowerCase().trim();
                counts[k] = (counts[k]||0) + 1;
            });
            // Update header count badge
            const cnt = document.getElementById("entry-count");
            if (cnt) cnt.textContent = items.length + " sighting" + (items.length!==1?"s":"") + " · " + Object.keys(counts).length + " object" + (Object.keys(counts).length!==1?"s":"");
            // Render every entry, newest first
            tb.innerHTML = items.map(m => {
                const k   = (m.object||"").toLowerCase().trim();
                const n   = counts[k]||1;
                const badge = n>1 ? ' <span style="background:#1e1a3a;color:#a89cf7;font-size:.7rem;padding:.1rem .35rem;border-radius:999px;border:1px solid #7c6af7">'+n+'×</span>' : "";
                const loc = m.location
                    ? '<span style="color:#7c6af7;font-size:.82rem">'+_e(m.location)+'</span>'
                    : '<span style="color:#444;font-style:italic;font-size:.78rem">—</span>';
                const desc = '<span style="font-size:.82rem;color:#888;display:block;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+_e(m.description||"")+'">'+_e(m.description||"")+'</span>';
                return "<tr><td>"+_e(m.object||"")+badge+"</td><td>"+loc+"</td><td>"+desc+"</td><td style='white-space:nowrap;font-size:.82rem'>"+_e(m.timestamp||"")+"</td></tr>";
            }).join("");
        }).catch(()=>{});
}
function _e(s) {
    return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

/* ═══════════════════════════════════════════════════════════════════════════
   SEND COMMAND
   ═══════════════════════════════════════════════════════════════════════════ */
async function sendCmd(transcript) {
    const now = Date.now();
    // Exit-search commands bypass ALL guards — user must always be able to exit
    const isExitCmd = ["done searching","stop searching","exit search"].includes(transcript);
    if (!isExitCmd) {
        if (transcript === lastCmdText && now - lastCmdTime < DEDUP_MS) {
            log("CMD",`Dedup: "${transcript}"`); return;
        }
        if (cmdInFlight) { log("CMD",`In-flight: "${transcript}"`); return; }
    } else {
        cmdInFlight = false;  // clear any stuck state
        window.VOICE_MODE = false;
    }

    lastCmdText = transcript;
    lastCmdTime = now;
    cmdInFlight = true;
    // Safety timeout — never leave cmdInFlight stuck forever
    setTimeout(() => { cmdInFlight = false; }, 7000);

    log("CMD", `→ "${transcript}"`);
    _setPanel("processing", "Processing...");

    try {
        const sr = await fetch("/api/voice/submit", {
            method:"POST", credentials:"same-origin",
            headers:{"Content-Type":"application/json"},
            body: JSON.stringify({command: transcript}),
        });
        const sd = await sr.json();
        if (sd.status === "queued") {
            const id = sd.command_id;
            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 200));
                const res  = await fetch(`/api/voice/result/${id}`, {credentials:"same-origin"});
                const data = await res.json();
                if (data.status === "done") { _dispatch(data); return; }
            }
        }
    } catch(e) { log("CMD_ERR", "Async: "+e); }

    // Sync fallback
    try {
        const r    = await fetch("/api/voice/command", {
            method:"POST", credentials:"same-origin",
            headers:{"Content-Type":"application/json"},
            body: JSON.stringify({command: transcript}),
        });
        _dispatch(await r.json());
    } catch(e) {
        log("CMD_ERR", "Sync: "+e);
        cmdInFlight = false;
        _setPanel("listening", _hint());
    }
}

function _dispatch(data) {
    cmdInFlight = false;
    const response = data.response || "";
    log("CMD", `← type=${data.type} "${response.slice(0,50)}"`);

    // Block mic for however long SAPI will take to speak this response.
    // Search results need longer blocking — user hears answer then thinks.
    const isSearchResult = (data.type === "location" || data.type === "not_found" || data.type === "ask");
    _blockMicFor(_sapiMuteMs(response, isSearchResult));

    if (data.type === "search_start") {
        window.VOICE_MODE = true;
        _setPanel("search", "Listening — say the object name");
    } else if (data.type === "search_end") {
        window.VOICE_MODE = false;
        _setPanel("listening", _hint());
        // Resume polling if it had been running — dashboard.html _handleVoiceResponse
        // decides whether to actually restart vision, but polling must be live
        if (!isPolling) startPolling();
    } else if (data.type === "stop") {
        window.VOICE_MODE = false;
        _setPanel("listening", _hint());
    } else if (data.type === "location" || data.type === "not_found") {
        // Result shown — STAY in search mode so the overlay remains visible.
        // User must say "done searching" to return to live captions.
        window.VOICE_MODE = true;
        _setPanel("search", "Say another object, or say \"done searching\"");
    } else if (data.type === "ask") {
        // Didn't understand — stay in search mode, show prompt
        window.VOICE_MODE = true;
        _setPanel("search", response.slice(0, 80));
    } else if (data.type === "empty") {
        // Nothing in memory — exit search mode, nothing to search
        window.VOICE_MODE = false;
        _setPanel("response", response.slice(0, 80));
        setTimeout(() => _setPanel("listening", _hint()), 4000);
    } else if (data.type === "start" || data.type === "already_running") {
        _setPanel("response", data.type === "already_running" ? "Resuming..." : "Starting up...");
        setTimeout(() => _setPanel("listening", _hint()), 3000);
    } else {
        _setPanel("response", response.slice(0, 80));
        setTimeout(() => { if (!window.VOICE_MODE) _setPanel("listening", _hint()); }, 3000);
    }

    if (typeof window._handleVoiceResponse === "function")
        window._handleVoiceResponse(data);
}

function _setPanel(state, text) {
    const p = document.getElementById("voice-command-panel");
    const l = document.getElementById("voice-status-text");
    if (p) p.className   = "voice-panel " + state;
    if (l) l.textContent = text || "";
}
function _hint() {
    return window.VOICE_MODE
        ? "Listening — say what you're looking for"
        : 'Say "start" · "stop" · "search" · "describe"';
}

/* ═══════════════════════════════════════════════════════════════════════════
   SPEECH RECOGNITION
   ═══════════════════════════════════════════════════════════════════════════ */

// Exit phrases — bypass mic block completely (must always work)
const EXIT_PHRASES = [
    "done searching", "stop searching", "exit search",
    "cancel search", "resume captions"
];

// Ordered command table — first match wins
// mode "any"    = works in both normal and search mode
// mode "normal" = only outside search mode
// mode "search" = only inside search mode
// send: null    = pass the full transcript to server
const CMD_TABLE = [
    // ── Start ─────────────────────────────────────────────────────────────
    { hear:"start captions", send:"start captions", mode:"normal" },
    { hear:"start vision",   send:"start captions", mode:"normal" },
    { hear:"begin",          send:"start captions", mode:"normal" },
    { hear:"start",          send:"start captions", mode:"normal" },

    // ── Stop ──────────────────────────────────────────────────────────────
    { hear:"stop captions",  send:"stop captions",  mode:"normal" },
    { hear:"stop vision",    send:"stop captions",  mode:"normal" },
    { hear:"pause",          send:"stop captions",  mode:"normal" },
    { hear:"stop",           send:"stop captions",  mode:"normal" },

    // ── Enter search ──────────────────────────────────────────────────────
    { hear:"search mode",    send:"search",          mode:"normal" },
    { hear:"start search",   send:"search",          mode:"normal" },
    { hear:"search",         send:"search",          mode:"normal" },

    // ── Exit search (belt + suspenders — EXIT_PHRASES check above is primary) ──
    { hear:"done searching", send:"done searching",  mode:"any"    },
    { hear:"stop searching", send:"done searching",  mode:"any"    },
    { hear:"exit search",    send:"done searching",  mode:"any"    },
    { hear:"resume",         send:"done searching",  mode:"search" },
    { hear:"done",           send:"done searching",  mode:"search" },

    // ── Utility ───────────────────────────────────────────────────────────
    { hear:"describe",       send:null, mode:"normal" },
    { hear:"what do you see",send:null, mode:"normal" },
    { hear:"tell me",        send:null, mode:"normal" },
    { hear:"save",           send:null, mode:"normal" },
    { hear:"remember",       send:null, mode:"normal" },
    { hear:"where is",       send:null, mode:"any"    },
    { hear:"where's",        send:null, mode:"any"    },
    { hear:"find my",        send:null, mode:"any"    },
    { hear:"locate",         send:null, mode:"any"    },
    { hear:"list memory",    send:null, mode:"normal" },
    { hear:"clear memory",   send:null, mode:"normal" },
];

function initVoiceCommands() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
        const btn = document.getElementById("mic-btn");
        if (btn) { btn.style.opacity="0.4"; btn.title="Voice requires Chrome or Edge"; }
        return;
    }
    // iOS: unlock AudioContext on first user gesture so TTS works immediately
    document.addEventListener("touchstart", function _unlock() {
        if (window.speechSynthesis) {
            const silent = new SpeechSynthesisUtterance(" ");
            silent.volume = 0;
            window.speechSynthesis.speak(silent);
        }
        document.removeEventListener("touchstart", _unlock);
    }, { once: true });

    const rec = new SR();
    window.VOICE_RECOGNITION = rec;
    rec.continuous     = true;
    rec.interimResults = true;
    rec.lang           = "en-US";

    rec.onstart = () => {
        const b = document.getElementById("mic-btn");
        if (b) b.classList.add("listening");
    };
    rec.onend = () => {
        const b = document.getElementById("mic-btn");
        if (b) b.classList.remove("listening");
        // Always restart — never let recognition die
        setTimeout(() => { try { rec.start(); } catch(_) {} }, 200);
    };
    rec.onerror = e => {
        log("MIC_ERR", e.error);
        // no-speech and aborted are normal — onend handles restart
    };

    rec.onresult = e => {
        let interim = "", finalT = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
            const txt = e.results[i][0].transcript.toLowerCase().trim();
            if (e.results[i].isFinal) finalT += txt + " ";
            else interim += txt;
        }

        // Show live transcript
        const lv = document.getElementById("voice-transcript");
        if (lv) lv.textContent = interim || finalT.trim();

        if (!finalT.trim()) return;
        // In search mode: only use the LAST final segment Chrome produced,
        // not the full accumulated transcript. Chrome with continuous=true
        // bleeds prior recognised words into later results (e.g. the user
        // said "cell" earlier, now says "bottle", Chrome returns "cell bottle").
        // Taking the last segment = the most recently spoken phrase only.
        let t;
        if (window.VOICE_MODE) {
            // Get only the last final result from this event batch
            let lastFinal = "";
            for (let i = e.resultIndex; i < e.results.length; i++) {
                if (e.results[i].isFinal)
                    lastFinal = e.results[i][0].transcript.toLowerCase().trim();
            }
            t = lastFinal || finalT.trim();
        } else {
            t = finalT.trim();
        }

        // ── STEP 1: Exit phrases always fire — mic block + dedup bypassed ─────
        if (EXIT_PHRASES.some(p => t.includes(p))) {
            log("VOICE", `Exit phrase: "${t}"`);
            window.VOICE_MODE = false;
            searchActive      = false;
            cmdInFlight       = false;   // clear any stuck in-flight command
            _blockMicFor(4000);
            // Use direct fetch — bypasses dedup/cmdInFlight guards entirely
            fetch("/api/voice/command", {
                method: "POST", credentials: "same-origin",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({command: "done searching"}),
            }).then(r => r.json()).then(d => {
                if (typeof window._handleVoiceResponse === "function")
                    window._handleVoiceResponse(d);
                _dispatch(d);
            }).catch(() => {});
            return;
        }

        // ── STEP 2: Mic block (SAPI echo protection) ─────────────────────────
        if (_micIsBlocked()) {
            log("VOICE", `Mic blocked — ignored: "${t}"`);
            return;
        }

        // ── STEP 2b: Discard very short or single-char results in search mode ─
        // Residual SAPI audio often produces single words or fragments.
        // Require at least 2 chars for search queries to filter these out.
        if (window.VOICE_MODE && t.length < 2) {
            log("VOICE", `Too short for search, ignored: "${t}"`);
            return;
        }

        const inSearch = window.VOICE_MODE;

        // ── STEP 3: In search mode — send raw utterance directly as query ─────
        // Do this BEFORE CMD_TABLE so "phone", "my keys", "glasses" etc. all
        // go straight to the server as search queries without being intercepted.
        if (inSearch && t.length > 1 && !cmdInFlight) {
            // Only let exit phrases through (already handled above) and
            // explicit stop commands — everything else is a search query.
            const isControl = ["stop","pause","done","resume"].includes(t.trim()) ||
                EXIT_PHRASES.some(p => t.includes(p));
            if (!isControl) {
                log("VOICE", `Search query (direct): "${t}"`);
                sendCmd(t);
                return;
            }
        }

        // ── STEP 4: Match CMD_TABLE (normal mode only beyond this point) ──────
        for (const entry of CMD_TABLE) {
            if (entry.mode === "normal" && inSearch)  continue;
            if (entry.mode === "search" && !inSearch) continue;
            if (!t.includes(entry.hear))              continue;

            const toSend = entry.send !== null ? entry.send : t;
            log("VOICE", `Hit: "${entry.hear}" → send "${toSend}"`);

            if (entry.send === "search" && !inSearch) {
                window.VOICE_MODE = true;
                _setPanel("search", "Say the object name clearly");
            }
            if (entry.send === "stop captions" || entry.send === "done searching") {
                window.VOICE_MODE = false;
            }

            sendCmd(toSend);
            return;
        }

        // ── STEP 5: Free utterance outside search mode ────────────────────────
        if (!inSearch && t.length > 2 && !cmdInFlight) {
            // Only forward if it looks like a where/find query
            const looksLikeQuery = ["where","find","locate","lost"].some(w => t.includes(w));
            if (looksLikeQuery) {
                log("VOICE", `Query (normal mode): "${t}"`);
                sendCmd(t);
                return;
            }
        }

        // ── STEP 5: Dashboard extra triggers (start/stop buttons) ────────────
        if (window.EXTRA_VOICE_TRIGGERS) {
            for (const [kw, fn] of Object.entries(window.EXTRA_VOICE_TRIGGERS)) {
                if (t.includes(kw)) { fn(); return; }
            }
        }
    };

    try { rec.start(); log("VOICE", "Listening started"); }
    catch(e) { log("VOICE_ERR", "Start failed: " + e); }
}

function toggleVoiceCommand() {
    if (!window.VOICE_RECOGNITION) { initVoiceCommands(); return; }
    if (window.VOICE_MODE) {
        window.VOICE_MODE = false;
        sendCmd("done searching");
    } else {
        window.VOICE_MODE = true;
        _setPanel("search", "Search mode — say what you're looking for");
        sendCmd("search");
    }
}

/* ─── Init ───────────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => { loadMemory(); });

/* ─── Exports ────────────────────────────────────────────────────────────── */
window.startPolling       = startPolling;
window.stopPolling        = stopPolling;
window.fetchCaption       = fetchCaption;
window.readAloud          = readAloud;
window.addToMemory        = addToMemory;
window.clearMemory        = clearMemory;
window.loadMemory         = loadMemory;
window.speakText          = speakText;
window.initVoiceCommands  = initVoiceCommands;
window.toggleVoiceCommand = toggleVoiceCommand;
window.sendVoiceCommand   = sendCmd;