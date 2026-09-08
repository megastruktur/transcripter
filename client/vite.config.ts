import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
// Client version for the X-Client header and the Settings panel — single
// source of truth is package.json (bumped by the release flow). The tsconfig
// has resolveJsonModule, so no node builtins are needed here (svelte-check
// checks this file in a DOM lib context without @types/node).
import pkg from './package.json';

export default defineConfig({
	plugins: [sveltekit()],
	clearScreen: false,
	define: {
		__APP_VERSION__: JSON.stringify(pkg.version)
	},
	server: {
		port: 5173,
		strictPort: true
	}
});
