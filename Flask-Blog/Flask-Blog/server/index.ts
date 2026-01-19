import { spawn } from 'child_process';
import path from 'path';

console.log("Starting Python Flask App...");

// Use absolute path to ensure we find app.py in the root
const appPath = path.join(process.cwd(), 'app.py');

// Spawn python process
const pythonProcess = spawn('python3', [appPath], { stdio: 'inherit' });

pythonProcess.on('close', (code) => {
  console.log(`Python process exited with code ${code}`);
  process.exit(code || 0);
});

pythonProcess.on('error', (err) => {
  console.error('Failed to start python process:', err);
});

// Handle termination signals
process.on('SIGTERM', () => {
    pythonProcess.kill('SIGTERM');
});

process.on('SIGINT', () => {
    pythonProcess.kill('SIGINT');
});
