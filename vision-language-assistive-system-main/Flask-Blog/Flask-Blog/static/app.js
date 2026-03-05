// Global State
let lastCaption = null;

// Speech Synthesis
function speakText(text) {
    if ('speechSynthesis' in window) {
        window.speechSynthesis.cancel(); // Stop previous
        const utterance = new SpeechSynthesisUtterance(text);
        
        // Settings
        if (window.USER_SETTINGS) {
            // Map 140-190 to rate 0.5 - 2.0 (approx)
            // 165 is default (1.0)
            const rate = (window.USER_SETTINGS.speech_rate - 115) / 50; 
            utterance.rate = Math.max(0.5, Math.min(2.0, rate));
        }
        
        window.speechSynthesis.speak(utterance);
    }
}

function readAloud() {
    if (lastCaption) {
        speakText(lastCaption.caption);
    }
}

// Polling Caption
async function fetchCaption() {
    try {
        const response = await fetch('/api/caption');
        const data = await response.json();
        
        // Update UI
        const captionEl = document.getElementById('caption-text');
        const tagsEl = document.getElementById('caption-tags');
        const timeEl = document.getElementById('caption-time');
        const lightEl = document.getElementById('status-light');
        const statusTextEl = document.getElementById('status-text');

        if (captionEl) {
            // Check if caption changed
            const isNew = !lastCaption || lastCaption.caption !== data.caption;
            
            captionEl.textContent = data.caption;
            tagsEl.innerHTML = data.objects.map(obj => `<span>${obj}</span>`).join('');
            timeEl.textContent = `Last updated: ${data.updated_at}`;
            
            // Status Light
            lightEl.className = 'status-light';
            if (data.status === 'LIVE') lightEl.classList.add('status-green');
            else if (data.status === 'SLOW') lightEl.classList.add('status-yellow');
            else lightEl.classList.add('status-red');
            
            statusTextEl.textContent = data.status;

            lastCaption = data;

            // Auto Speak
            if (isNew && window.USER_SETTINGS && window.USER_SETTINGS.auto_speak) {
                speakText(data.caption);
            }
        }
    } catch (e) {
        console.error("Failed to fetch caption", e);
        const lightEl = document.getElementById('status-light');
        if (lightEl) {
            lightEl.className = 'status-light status-red';
            document.getElementById('status-text').textContent = "ERROR";
        }
    }
}

async function addToMemory() {
    if (!lastCaption) return;
    
    // Pick the first object as the "main" object for simplicity or join them
    const mainObject = lastCaption.objects.length > 0 ? lastCaption.objects[0] : "Object";
    
    await fetch('/api/memory/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            object: mainObject, // In a real app, user might select which object
            description: lastCaption.caption,
            timestamp: lastCaption.updated_at
        })
    });
    
    speakText("Added to memory.");
}

async function clearMemory() {
    if (confirm("Are you sure you want to clear your memory?")) {
        await fetch('/api/memory/clear', { method: 'POST' });
        window.location.reload();
    }
}

// Start polling if on dashboard
if (document.getElementById('caption-text')) {
    fetchCaption();
    setInterval(fetchCaption, 2000);
}
