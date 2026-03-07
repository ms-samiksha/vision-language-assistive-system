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
const POLL_MS    = 3000;
const BG_POLL_MS = 8000;

// Estimate SAPI speaking time: ~130 words/min = ~2.2 chars/sec
// We add a 1.5s buffer on top so the mic opens slightly after SAPI stops
function _sapiMuteMs(text) {
    const words = (text || "").split(/\s+/).length;
    const speakMs = (words / 130) * 60 * 1000;
    return Math.max(1800, speakMs + 1500);
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
   TTS (browser — for instant local feedback only, not for SAPI captions)
   ═══════════════════════════════════════════════════════════════════════════ */
function speakText(text, delayMs) {
    // speakText is used for instant local voice feedback BEFORE server responds.
    // The actual captions are spoken by Windows SAPI on the server side.
    if (delayMs === undefined) delayMs = 0;
    if (!text || !text.trim()) return;
    if (!window.speechSynthesis) return;
    setTimeout(() => {
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(text);
        u.rate  = 1.0;
        u.pitch = 1.0;
        window.speechSynthesis.speak(u);
    }, delayMs);
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
            tb.innerHTML = items.length
                ? items.map(m=>`<tr>
                    <td>${_e(m.object||"")}</td>
                    <td>${_e(m.description||"")}</td>
                    <td>${_e(m.timestamp||"")}</td></tr>`).join("")
                : '<tr><td colspan="3">No memories saved yet.</td></tr>';
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
    if (transcript === lastCmdText && now - lastCmdTime < DEDUP_MS) {
        log("CMD",`Dedup: "${transcript}"`); return;
    }
    if (cmdInFlight) { log("CMD",`In-flight: "${transcript}"`); return; }

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

    // Block mic for however long SAPI will take to speak this response
    _blockMicFor(_sapiMuteMs(response));

    if (data.type === "search_start") {
        window.VOICE_MODE = true;
        _setPanel("search", "Listening — say what you're looking for");
    } else if (data.type === "search_end" || data.type === "stop") {
        window.VOICE_MODE = false;
        _setPanel("listening", _hint());
    } else if (data.type === "start") {
        _setPanel("response", "Starting up...");
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
        const t = finalT.trim();

        // ── STEP 1: Exit phrases always fire — mic block bypassed ────────────
        if (EXIT_PHRASES.some(p => t.includes(p))) {
            log("VOICE", `Exit phrase: "${t}"`);
            window.VOICE_MODE = false;
            _blockMicFor(3000);   // block for SAPI "Resuming..." response
            sendCmd("done searching");
            return;
        }

        // ── STEP 2: Mic block (SAPI echo protection) ─────────────────────────
        if (_micIsBlocked()) {
            log("VOICE", `Mic blocked — ignored: "${t}"`);
            return;
        }

        const inSearch = window.VOICE_MODE;

        // ── STEP 3: Match CMD_TABLE in order ─────────────────────────────────
        for (const entry of CMD_TABLE) {
            if (entry.mode === "normal" && inSearch)  continue;
            if (entry.mode === "search" && !inSearch) continue;
            if (!t.includes(entry.hear))              continue;

            const toSend = entry.send !== null ? entry.send : t;
            log("VOICE", `Hit: "${entry.hear}" → send "${toSend}"`);

            // Instant local state update before server responds
            if (entry.send === "search" && !inSearch) {
                window.VOICE_MODE = true;
                _setPanel("search", "Search mode — say what you're looking for");
            }
            if (entry.send === "stop captions" || entry.send === "done searching") {
                window.VOICE_MODE = false;
            }

            sendCmd(toSend);
            return;
        }

        // ── STEP 4: In search mode — full utterance is the query ─────────────
        if (inSearch && t.length > 2 && !cmdInFlight) {
            log("VOICE", `Search query: "${t}"`);
            sendCmd(t);
            return;
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