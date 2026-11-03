import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
const backend = 'http://127.0.0.1:8000';
export default defineConfig({plugins:[react()],server:{proxy:{'/api':backend,'/health':backend,'/ws':{target:backend,ws:true},'/worklet.js':backend,'/pcm-player.js':backend}},build:{chunkSizeWarningLimit:1000}});
